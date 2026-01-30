"""
FreeCAD to USD Exporter.

Exports FreeCAD documents to Universal Scene Description (USD) format.
Uses BFS traversal with a handler registry for different object types.
"""

import sys
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Tuple

import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Gf, Sdf

# Import sibling modules (FreeCAD puts Mod folder on sys.path)
import Materials
import Geometry


# ----------------------------
# Configuration
# ----------------------------

UNBAKE_POINTS_TO_LOCAL = True


# ----------------------------
# Export context
# ----------------------------

@dataclass
class ExportContext:
    """Context passed to each exporter handler."""
    stage: Usd.Stage
    parent_xform: UsdGeom.Xform
    parent_global: FreeCAD.Placement
    is_prototype_root: bool = False


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


def _find_handler(type_id: str) -> Callable:
    """Find the appropriate handler for a given TypeId."""
    # Sort by priority descending, then by prefix length descending (more specific first)
    candidates = [
        (prefix, priority, handler)
        for prefix, (priority, handler) in EXPORTERS.items()
        if type_id.startswith(prefix) or prefix == "_fallback"
    ]
    candidates.sort(key=lambda x: (-x[1], -len(x[0])))

    for prefix, priority, handler in candidates:
        if type_id.startswith(prefix):
            return handler

    # Return fallback if registered, otherwise None
    if "_fallback" in EXPORTERS:
        return EXPORTERS["_fallback"][1]
    return None


# ----------------------------
# Caches
# ----------------------------

PROTOTYPE_CACHE = {}  # key: (docName, targetName) -> Sdf.Path
PROTOTYPES_ROOT_PATH = Sdf.Path("/Scene/__Prototypes")


# ----------------------------
# Utilities
# ----------------------------

def _make_usd_safe(name: str) -> str:
    """Sanitize a name for use as a USD prim name."""
    name = (name or "").strip() or "Object"
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name[0].isdigit():
        name = "_" + name
    return name


def _get_children(obj):
    """
    Get child objects for traversal.
    Normal objects: Group + OutList.
    Links: do NOT traverse OutList (proxy/dependency graph), only Group if any.
    """
    children = []
    seen = set()

    if hasattr(obj, "Group") and obj.Group:
        for ch in obj.Group:
            if id(ch) not in seen:
                children.append(ch)
                seen.add(id(ch))

    if getattr(obj, "TypeId", "").startswith("App::Link"):
        return children

    if hasattr(obj, "OutList"):
        for ch in obj.OutList:
            if id(ch) not in seen:
                children.append(ch)
                seen.add(id(ch))

    return children


def _get_visible_children(obj):
    """Get visible children for traversal."""
    children = []
    for child in _get_children(obj):
        vo = getattr(child, "ViewObject", None)
        if vo and not vo.Visibility:
            continue
        children.append(child)
    return children


def _get_global_placement(obj, parent_global: FreeCAD.Placement) -> FreeCAD.Placement:
    """Get the global placement of an object."""
    if hasattr(obj, "getGlobalPlacement"):
        try:
            return obj.getGlobalPlacement()
        except Exception:
            pass

    if hasattr(obj, "Placement"):
        try:
            return parent_global.multiply(obj.Placement)
        except Exception:
            pass

    return parent_global


def _placement_to_usd_ops(xform: UsdGeom.Xform, pl: FreeCAD.Placement):
    """Author local transform using translate + orient ops."""
    pos = pl.Base
    q = pl.Rotation.Q  # (x, y, z, w)

    xform.ClearXformOpOrder()
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[3], q[0], q[1], q[2]))


# ----------------------------
# Link / prototype helpers
# ----------------------------

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


def _get_prototypes_root(stage: Usd.Stage) -> UsdGeom.Xform:
    """Create or get the hidden __Prototypes container."""
    xf = UsdGeom.Xform.Define(stage, str(PROTOTYPES_ROOT_PATH))
    UsdGeom.Imageable(xf.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    xf.GetPrim().SetMetadata("hidden", True)
    return xf


def _prototype_key_for_target(target_obj) -> Tuple[str, str]:
    """Create a cache key for a prototype target."""
    doc = getattr(target_obj, "Document", None)
    doc_name = getattr(doc, "Name", "Doc")
    return (doc_name, getattr(target_obj, "Name", "Obj"))


def _get_single_link_child(obj):
    """Detect wrapper with exactly one App::Link child."""
    kids = []
    if hasattr(obj, "Group") and obj.Group:
        kids.extend(obj.Group)
    if hasattr(obj, "OutList") and obj.OutList:
        kids.extend(obj.OutList)

    seen = set()
    uniq = [k for k in kids if not (id(k) in seen or seen.add(id(k)))]

    link_kids = [k for k in uniq if getattr(k, "TypeId", "").startswith("App::Link")]
    return link_kids[0] if len(link_kids) == 1 else None


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


def _ensure_prototype_for_target(target_obj, stage: Usd.Stage) -> Sdf.Path:
    """Export target hierarchy under __Prototypes using BFS, return its path."""
    key = _prototype_key_for_target(target_obj)
    if key in PROTOTYPE_CACHE:
        return PROTOTYPE_CACHE[key]

    protos_root = _get_prototypes_root(stage)
    proto_name = "Proto_" + _make_usd_safe(getattr(target_obj, "Label", "") or target_obj.Name)
    proto_path = protos_root.GetPath().AppendChild(proto_name)

    if stage.GetPrimAtPath(proto_path):
        PROTOTYPE_CACHE[key] = proto_path
        return proto_path

    proto_xf = UsdGeom.Xform.Define(stage, proto_path)
    proto_xf.ClearXformOpOrder()

    target_global = _get_global_placement(target_obj, FreeCAD.Placement())

    # BFS export for prototype subtree
    ctx = ExportContext(
        stage=stage,
        parent_xform=proto_xf,
        parent_global=target_global,
        is_prototype_root=True
    )
    _export_bfs([(target_obj, ctx)])

    PROTOTYPE_CACHE[key] = proto_path
    return proto_path


# ----------------------------
# Type-specific exporters
# ----------------------------

@exporter("App::Link", priority=100)
def _export_link(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export App::Link as USD instance reference."""
    if ctx.is_prototype_root:
        return True  # Inside prototype, treat as regular object

    target = _resolve_link_chain(obj)
    if target:
        proto_path = _ensure_prototype_for_target(target, ctx.stage)
        prim = this_xform.GetPrim()
        prim.GetReferences().AddInternalReference(proto_path)
        prim.SetInstanceable(True)
    return False  # Don't recurse


@exporter("Part::FeaturePython", priority=90)
def _export_feature_python(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export FeaturePython - check if it's a clone pointing to a link."""
    if not ctx.is_prototype_root:
        link_obj = _resolve_to_link_if_any(obj)
        if link_obj is not None:
            target = _resolve_link_chain(link_obj)
            if target:
                proto_path = _ensure_prototype_for_target(target, ctx.stage)
                prim = this_xform.GetPrim()
                prim.GetReferences().AddInternalReference(proto_path)
                prim.SetInstanceable(True)
            return False

    # Otherwise treat as Part::Feature
    return _export_part_feature(obj, ctx, this_xform, child_global, usd_material)


@exporter("PartDesign::Body", priority=80)
def _export_body(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
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
    return False  # Don't recurse into PartDesign features


@exporter("Mesh::Feature", priority=70)
def _export_mesh_feature(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
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
def _export_part_feature(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
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
def _export_app_part(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export App::Part as container only."""
    return True


@exporter("App::DocumentObjectGroup", priority=50)
def _export_group(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Export group as container only."""
    return True


@exporter("_fallback", priority=-1000)
def _export_fallback(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, child_global: FreeCAD.Placement, usd_material) -> bool:
    """Fallback exporter - try Mesh, then Shape, or treat as container."""
    type_id = getattr(obj, "TypeId", "")
    mesh_name = _make_usd_safe(getattr(obj, "Label", "") or obj.Name) + "_mesh"

    # Skip if it's a group-like object
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


# ----------------------------
# Link wrapper detection (pre-handler check)
# ----------------------------

def _handle_link_wrapper(obj, ctx: ExportContext, this_xform: UsdGeom.Xform, type_id: str) -> bool:
    """
    Check if obj is a wrapper containing a single App::Link child.
    If so, create instance reference and return True (handled).
    """
    if ctx.is_prototype_root:
        return False
    if type_id.startswith("App::Link"):
        return False

    link_child = _get_single_link_child(obj)
    if link_child is not None:
        target = _resolve_link_chain(link_child)
        if target:
            proto_path = _ensure_prototype_for_target(target, ctx.stage)
            prim = this_xform.GetPrim()
            prim.GetReferences().AddInternalReference(proto_path)
            prim.SetInstanceable(True)
        return True
    return False


# ----------------------------
# BFS Export
# ----------------------------

def _export_bfs(initial_queue: list):
    """
    Export objects using BFS traversal.

    Queue items: (obj, ExportContext)
    """
    queue = deque(initial_queue)

    while queue:
        obj, ctx = queue.popleft()

        type_id = getattr(obj, "TypeId", "")
        label = getattr(obj, "Label", "") or getattr(obj, "Name", "")
        usd_name = _make_usd_safe(label)

        FreeCAD.Console.PrintMessage(
            f"[USD] Exporting: Label='{label}'  Name='{getattr(obj,'Name','')}'  TypeId='{type_id}'\n"
        )

        # Create Xform for this object
        this_path = ctx.parent_xform.GetPath().AppendChild(usd_name)
        this_xform = UsdGeom.Xform.Define(ctx.stage, this_path)

        # Compute placements
        child_global = _get_global_placement(obj, ctx.parent_global)

        if ctx.is_prototype_root:
            local_to_parent = FreeCAD.Placement()
        else:
            local_to_parent = ctx.parent_global.inverse().multiply(child_global)

        _placement_to_usd_ops(this_xform, local_to_parent)

        # Extract material
        mat_props = Materials.extract_material_properties(obj)
        usd_material = Materials.ensure_material(ctx.stage, mat_props) if mat_props else None

        # Check for link wrapper pattern first
        if _handle_link_wrapper(obj, ctx, this_xform, type_id):
            continue  # Handled as instance, don't recurse

        # Find and call the appropriate handler
        handler = _find_handler(type_id)
        if handler:
            should_recurse = handler(obj, ctx, this_xform, child_global, usd_material)
        else:
            should_recurse = True

        # Enqueue children if handler says to recurse
        if should_recurse:
            child_ctx = ExportContext(
                stage=ctx.stage,
                parent_xform=this_xform,
                parent_global=child_global,
                is_prototype_root=False  # Children are never prototype roots
            )
            for child in _get_visible_children(obj):
                queue.append((child, child_ctx))


# ----------------------------
# Export entry point
# ----------------------------

def export(objects, filename: str):
    """
    Export FreeCAD objects to a USD file.

    Args:
        objects: List of FreeCAD objects to export, or None for all objects
        filename: Output USD file path (.usd, .usda, .usdc)
    """
    PROTOTYPE_CACHE.clear()
    Materials.clear_cache()

    if not objects:
        doc = FreeCAD.ActiveDocument
        objects = doc.Objects

    stage = Usd.Stage.CreateNew(filename)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    root = UsdGeom.Xform.Define(stage, "/Scene")
    identity = FreeCAD.Placement()

    # Build initial queue
    initial_ctx = ExportContext(
        stage=stage,
        parent_xform=root,
        parent_global=identity,
        is_prototype_root=False
    )
    initial_queue = [(obj, initial_ctx) for obj in objects]

    # Run BFS export
    _export_bfs(initial_queue)

    stage.GetRootLayer().Save()
