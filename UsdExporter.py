"""
FreeCAD to USD Exporter.

Exports FreeCAD documents to Universal Scene Description (USD) format.
"""

import sys
import os
import re

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
# Prototype cache
# ----------------------------

PROTOTYPE_CACHE = {}  # key: (docName, targetName) -> Sdf.Path
PROTOTYPES_ROOT_PATH = Sdf.Path("/Scene/__Prototypes")


# ----------------------------
# Export entry point
# ----------------------------

def export(objects, filename):
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
    for obj in objects:
        _export_object_recursive(obj, root, stage, parent_global=identity, is_prototype_root=False)

    stage.GetRootLayer().Save()


# ----------------------------
# Utilities
# ----------------------------

def _make_usd_safe(name):
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

    # Avoid OutList recursion for links (prevents proxy graph explosions)
    if getattr(obj, "TypeId", "").startswith("App::Link"):
        return children

    if hasattr(obj, "OutList"):
        for ch in obj.OutList:
            if id(ch) not in seen:
                children.append(ch)
                seen.add(id(ch))

    return children


def _get_global_placement(obj, parent_global):
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


def _placement_to_usd_ops(xform, pl):
    """
    Author local transform for a prim using translate + orient ops.

    Note: FreeCAD quaternion is (x,y,z,w), USD Gf.Quatf expects (w,x,y,z).
    """
    pos = pl.Base
    q = pl.Rotation.Q  # (x, y, z, w)

    xform.ClearXformOpOrder()
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[3], q[0], q[1], q[2]))  # w, x, y, z


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
    """
    Follow App::Link -> LinkedObject -> ... until non-link.
    Returns final target (or None if broken).
    """
    cur = obj
    seen = set()
    while cur and getattr(cur, "TypeId", "").startswith("App::Link"):
        if id(cur) in seen:
            break
        seen.add(id(cur))
        cur = _get_linked_object(cur)
    return cur


def _get_prototypes_root(stage):
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


def _ensure_prototype_for_target(target_obj, stage):
    """
    Export the target hierarchy once under /Scene/__Prototypes and return its Sdf.Path.
    """
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

    _export_object_recursive(
        target_obj,
        proto_xf,
        stage,
        parent_global=target_global,
        is_prototype_root=True
    )

    PROTOTYPE_CACHE[key] = proto_path
    return proto_path


def _get_single_link_child(obj):
    """
    Detect wrapper: object that isn't App::Link but has exactly one App::Link child.
    """
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
    """
    Try common properties used by FreeCAD clone-like FeaturePython objects.
    Returns referenced base object if found.
    """
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


def _resolve_to_link_if_any(obj, max_hops=6):
    """
    If obj is a link, returns it.
    If obj is a wrapper/clone whose base ultimately points to a link, returns that link.
    Otherwise returns None.
    """
    cur = obj
    seen = set()
    for _ in range(max_hops):
        if cur is None:
            return None
        if id(cur) in seen:
            return None
        seen.add(id(cur))

        tid = getattr(cur, "TypeId", "")
        if tid.startswith("App::Link"):
            return cur

        base = _get_clone_base_object(cur)
        if base is None:
            return None

        cur = base
    return None


# ----------------------------
# Main recursive exporter
# ----------------------------

def _export_object_recursive(obj, parent_xform, stage, parent_global, is_prototype_root=False):
    """Recursively export a FreeCAD object and its children to USD."""
    type_id = getattr(obj, "TypeId", "")
    label = getattr(obj, "Label", "") or getattr(obj, "Name", "")
    usd_name = _make_usd_safe(label)

    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{label}'  Name='{getattr(obj,'Name','')}'  TypeId='{type_id}'\n"
    )

    # Create Xform for this object
    this_path = parent_xform.GetPath().AppendChild(usd_name)
    this_xform = UsdGeom.Xform.Define(stage, this_path)

    # Compute placements
    child_global = _get_global_placement(obj, parent_global)

    if is_prototype_root:
        local_to_parent = FreeCAD.Placement()
    else:
        local_to_parent = parent_global.inverse().multiply(child_global)

    _placement_to_usd_ops(this_xform, local_to_parent)

    # Extract material
    mat_props = Materials.extract_material_properties(obj)
    usd_material = Materials.ensure_material(stage, mat_props) if mat_props else None

    # --- Handle link/clone instances ---
    if not is_prototype_root:
        # Single App::Link child wrapper
        link_child = _get_single_link_child(obj)
        if link_child is not None and not type_id.startswith("App::Link"):
            target = _resolve_link_chain(link_child)
            if target:
                proto_path = _ensure_prototype_for_target(target, stage)
                prim = this_xform.GetPrim()
                prim.GetReferences().AddInternalReference(proto_path)
                prim.SetInstanceable(True)
            return

        # FeaturePython clone pointing to App::Link
        if type_id.startswith("Part::FeaturePython"):
            link_obj = _resolve_to_link_if_any(obj)
            if link_obj is not None:
                target = _resolve_link_chain(link_obj)
                if target:
                    proto_path = _ensure_prototype_for_target(target, stage)
                    prim = this_xform.GetPrim()
                    prim.GetReferences().AddInternalReference(proto_path)
                    prim.SetInstanceable(True)
                return

        # Direct App::Link
        if type_id.startswith("App::Link"):
            target = _resolve_link_chain(obj)
            if target:
                proto_path = _ensure_prototype_for_target(target, stage)
                prim = this_xform.GetPrim()
                prim.GetReferences().AddInternalReference(proto_path)
                prim.SetInstanceable(True)
            return

    # --- Handle geometry ---
    mesh_name = usd_name + "_mesh"

    if type_id == "PartDesign::Body":
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            Geometry.tessellate_shape_to_usd(
                obj.Shape, stage, this_xform, mesh_name,
                tess_tolerance=0.1,
                angle_threshold=10.0,
                unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
                global_placement=child_global,
                material=usd_material,
            )
        return  # Don't recurse into PartDesign features

    elif type_id.startswith("Mesh::Feature"):
        if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None):
            Geometry.mesh_to_usd(
                obj.Mesh, stage, this_xform, mesh_name,
                angle_threshold=10.0,
                material=usd_material,
            )

    elif type_id.startswith("Part::Feature"):
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            Geometry.tessellate_shape_to_usd(
                obj.Shape, stage, this_xform, mesh_name,
                tess_tolerance=0.1,
                angle_threshold=10.0,
                unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
                global_placement=child_global,
                material=usd_material,
            )

    elif not type_id.startswith("App::Part") and "Group" not in type_id:
        # Fallback: try Mesh, then Shape
        if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None):
            Geometry.mesh_to_usd(
                obj.Mesh, stage, this_xform, mesh_name,
                angle_threshold=10.0,
                material=usd_material,
            )
        elif hasattr(obj, "Shape") and not obj.Shape.isNull():
            Geometry.tessellate_shape_to_usd(
                obj.Shape, stage, this_xform, mesh_name,
                tess_tolerance=0.1,
                angle_threshold=10.0,
                unbake_to_local=UNBAKE_POINTS_TO_LOCAL,
                global_placement=child_global,
                material=usd_material,
            )

    # Recurse into children
    for child in _get_children(obj):
        vo = getattr(child, "ViewObject", None)
        if vo and not vo.Visibility:
            continue
        _export_object_recursive(child, this_xform, stage, parent_global=child_global, is_prototype_root=False)
