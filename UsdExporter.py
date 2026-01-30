"""
FreeCAD to USD Exporter.

Exports FreeCAD documents to Universal Scene Description (USD) format.
Uses BFS traversal with a handler registry for different object types.
"""

import sys
import os
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable

import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Gf, Sdf

# Import sibling modules
import Materials
import Registry


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
    ensure_prototype: Optional[Callable] = None


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
# Prototype helpers
# ----------------------------

def _get_prototypes_root(stage: Usd.Stage) -> UsdGeom.Xform:
    """Create or get the hidden __Prototypes container."""
    xf = UsdGeom.Xform.Define(stage, str(PROTOTYPES_ROOT_PATH))
    UsdGeom.Imageable(xf.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    xf.GetPrim().SetMetadata("hidden", True)
    return xf


def _prototype_key_for_target(target_obj):
    """Create a cache key for a prototype target."""
    doc = getattr(target_obj, "Document", None)
    doc_name = getattr(doc, "Name", "Doc")
    return (doc_name, getattr(target_obj, "Name", "Obj"))


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
        is_prototype_root=True,
        ensure_prototype=_ensure_prototype_for_target,
    )
    _export_bfs([(target_obj, ctx)])

    PROTOTYPE_CACHE[key] = proto_path
    return proto_path


# ----------------------------
# Link wrapper detection
# ----------------------------

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
        target = Registry._resolve_link_chain(link_child)
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
            continue

        # Find and call the appropriate handler from registry
        handler = Registry.find_handler(type_id)
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
                is_prototype_root=False,
                ensure_prototype=ctx.ensure_prototype,
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

    # Build initial queue with context
    initial_ctx = ExportContext(
        stage=stage,
        parent_xform=root,
        parent_global=identity,
        is_prototype_root=False,
        ensure_prototype=_ensure_prototype_for_target,
    )
    initial_queue = [(obj, initial_ctx) for obj in objects]

    # Run BFS export
    _export_bfs(initial_queue)

    stage.GetRootLayer().Save()
