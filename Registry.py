"""
Exporter handler registry for FreeCAD object types.

Handlers are registered with @exporter decorator and looked up by TypeId prefix.
"""

import sys
import os
from typing import Callable, Dict, Tuple

import FreeCAD

# Ensure USD Python bindings (pxr) are visible
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import UsdGeom

import Billboard
import Geometry
import Prototypes


# ----------------------------
# Configuration
# ----------------------------

UNBAKE_POINTS_TO_LOCAL = True


# ----------------------------
# Handler registry
# ----------------------------

# Maps type_prefix -> (priority, handler_func)
# Higher priority handlers are checked first
EXPORTERS: Dict[str, Tuple[int, Callable]] = {}


def exporter(type_prefix: str, priority: int = 0):
    """Decorator to register an exporter for a FreeCAD type prefix."""
    def decorator(fn):
        EXPORTERS[type_prefix] = (priority, fn)
        return fn
    return decorator


def find_handler(type_id: str) -> Callable:
    """Find the appropriate handler for a given TypeId."""
    candidates = [
        (prefix, priority, handler)
        for prefix, (priority, handler) in EXPORTERS.items()
        if type_id.startswith(prefix) or prefix == "_fallback"
    ]
    candidates.sort(key=lambda x: (-x[1], -len(x[0])))

    for prefix, priority, handler in candidates:
        if type_id.startswith(prefix):
            return handler

    if "_fallback" in EXPORTERS:
        return EXPORTERS["_fallback"][1]
    return None


# ----------------------------
# Helper functions
# ----------------------------

def _make_usd_safe(name: str) -> str:
    """Sanitize a name for use as a USD prim name."""
    import re
    name = (name or "").strip() or "Object"
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name[0].isdigit():
        name = "_" + name
    return name


def _get_linked_object(obj):
    """Get the linked object from an App::Link."""
    if hasattr(obj, "LinkedObject"):
        return obj.LinkedObject
    if hasattr(obj, "Link"):
        return obj.Link
    return None


def _resolve_link_chain(obj):
    """Follow App::Link chain until non-link. Returns final target or None."""
    cur = obj
    seen = set()
    while cur and getattr(cur, "TypeId", "").startswith("App::Link"):
        if id(cur) in seen:
            break
        seen.add(id(cur))
        cur = _get_linked_object(cur)
    return cur


def _get_clone_base_object(obj):
    """Get base object from clone-like FeaturePython objects."""
    candidates = ["Base", "Object", "Source", "Objects", "LinkedObject", "Link"]
    for prop in candidates:
        if not hasattr(obj, prop):
            continue
        try:
            v = getattr(obj, prop)
        except Exception:
            continue
        if v is None:
            continue

        if isinstance(v, (list, tuple)):
            if len(v) == 0:
                continue
            v0 = v[0]
            if isinstance(v0, (list, tuple)) and len(v0) > 0:
                v0 = v0[0]
            return v0
        return v
    return None


def _resolve_to_link_if_any(obj, max_hops: int = 6):
    """If obj or its base chain points to an App::Link, return that link."""
    cur = obj
    seen = set()
    for _ in range(max_hops):
        if cur is None:
            return None
        if id(cur) in seen:
            return None
        seen.add(id(cur))

        if getattr(cur, "TypeId", "").startswith("App::Link"):
            return cur

        base = _get_clone_base_object(cur)
        if base is None:
            return None
        cur = base
    return None


# ----------------------------
# Type-specific exporters
#
# Each handler signature:
#   handler(obj, ctx, this_xform, child_global, usd_material) -> bool
#
# Returns True if children should be traversed, False otherwise.
# ctx.ensure_prototype(target, stage) is available for creating prototypes.
# ----------------------------

@exporter("App::Link", priority=100)
def _export_link(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export App::Link as USD instance reference."""
    if ctx.is_prototype_root:
        return True  # Inside prototype, treat as regular object

    target = _resolve_link_chain(obj)
    if target and ctx.ensure_prototype:
        proto_path = ctx.ensure_prototype(target, ctx.stage)
        Prototypes.make_instance(this_xform.GetPrim(), proto_path)
    return False


@exporter("Part::FeaturePython", priority=90)
def _export_feature_python(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export FeaturePython - check if it's a clone pointing to a link."""
    if not ctx.is_prototype_root:
        link_obj = _resolve_to_link_if_any(obj)
        if link_obj is not None:
            target = _resolve_link_chain(link_obj)
            if target and ctx.ensure_prototype:
                proto_path = ctx.ensure_prototype(target, ctx.stage)
                Prototypes.make_instance(this_xform.GetPrim(), proto_path)
            return False

    # Otherwise treat as Part::Feature
    return _export_part_feature(obj, ctx, this_xform, child_global, usd_material)


@exporter("PartDesign::Body", priority=80)
def _export_body(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export PartDesign::Body - single mesh, no child recursion."""
    if hasattr(obj, "Shape") and not obj.Shape.isNull():
        mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"
        Geometry.tessellate_shape_to_usd(
            obj.Shape, ctx.stage, this_xform, mesh_name,
            tess_tolerance=0.1,
            angle_threshold=10.0,
            unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
            global_placement=child_global,
            material=usd_material,
        )
    return False


@exporter("Mesh::Feature", priority=70)
def _export_mesh_feature(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export Mesh::Feature using existing triangulation."""
    if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None):
        mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"
        Geometry.mesh_to_usd(
            obj.Mesh, ctx.stage, this_xform, mesh_name,
            angle_threshold=10.0,
            material=usd_material,
        )
    return True


@exporter("Part::Feature", priority=60)
def _export_part_feature(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export Part::Feature by tessellating its Shape."""
    if hasattr(obj, "Shape") and not obj.Shape.isNull():
        mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"
        Geometry.tessellate_shape_to_usd(
            obj.Shape, ctx.stage, this_xform, mesh_name,
            tess_tolerance=0.1,
            angle_threshold=10.0,
            unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
            global_placement=child_global,
            material=usd_material,
        )
    return True


@exporter("App::Part", priority=50)
def _export_app_part(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export App::Part as container only."""
    return True


@exporter("App::DocumentObjectGroup", priority=50)
def _export_group(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export group as container only."""
    return True


@exporter("App::FeaturePython", priority=55)
def _export_app_feature_python(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export App::FeaturePython - handles TextBillboard, falls through otherwise."""
    if Billboard.is_text_billboard(obj):
        Billboard.export_text_billboard(obj, this_xform)
        return False

    # Generic: try shape, then treat as container
    if hasattr(obj, "Shape") and not obj.Shape.isNull():
        mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"
        Geometry.tessellate_shape_to_usd(
            obj.Shape, ctx.stage, this_xform, mesh_name,
            tess_tolerance=0.1,
            angle_threshold=10.0,
            unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
            global_placement=child_global,
            material=usd_material,
        )
    return True


@exporter("_fallback", priority=-1000)
def _export_fallback(obj, ctx, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Fallback exporter - try Mesh, then Shape, or treat as container."""
    type_id = getattr(obj, "TypeId", "")
    mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"

    if "Group" in type_id:
        return True

    if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None):
        Geometry.mesh_to_usd(
            obj.Mesh, ctx.stage, this_xform, mesh_name,
            angle_threshold=10.0,
            material=usd_material,
        )
    elif hasattr(obj, "Shape") and not obj.Shape.isNull():
        Geometry.tessellate_shape_to_usd(
            obj.Shape, ctx.stage, this_xform, mesh_name,
            tess_tolerance=0.1,
            angle_threshold=10.0,
            unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
            global_placement=child_global,
            material=usd_material,
        )
    return True
