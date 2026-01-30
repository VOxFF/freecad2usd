"""
USD Prototype/Instance management for FreeCAD links.

Handles creation and caching of prototype prims under /Scene/__Prototypes.
"""

import sys
import os

import FreeCAD

# Ensure USD Python bindings (pxr) are visible
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Sdf


# ----------------------------
# Cache and constants
# ----------------------------

PROTOTYPE_CACHE = {}  # key: (docName, targetName) -> Sdf.Path
PROTOTYPES_ROOT_PATH = Sdf.Path("/Scene/__Prototypes")


def clear_cache():
    """Clear the prototype cache. Call at the start of each export."""
    PROTOTYPE_CACHE.clear()


# ----------------------------
# Utilities
# ----------------------------

def _make_usd_safe(name: str) -> str:
    """Sanitize a name for use as a USD prim name."""
    import re
    name = (name or "").strip() or "Object"
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name[0].isdigit():
        name = "_" + name
    return name


def _get_prototypes_root(stage: Usd.Stage) -> UsdGeom.Xform:
    """Create or get the hidden __Prototypes container."""
    xf = UsdGeom.Xform.Define(stage, str(PROTOTYPES_ROOT_PATH))
    UsdGeom.Imageable(xf.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    xf.GetPrim().SetMetadata("hidden", True)
    return xf


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


def prototype_key(target_obj) -> tuple:
    """Create a cache key for a prototype target."""
    doc = getattr(target_obj, "Document", None)
    doc_name = getattr(doc, "Name", "Doc")
    return (doc_name, getattr(target_obj, "Name", "Obj"))


def is_cached(target_obj) -> bool:
    """Check if a prototype for this target already exists in cache."""
    return prototype_key(target_obj) in PROTOTYPE_CACHE


def get_cached_path(target_obj) -> Sdf.Path:
    """Get cached prototype path. Returns None if not cached."""
    return PROTOTYPE_CACHE.get(prototype_key(target_obj))


# ----------------------------
# Prototype creation
# ----------------------------

def ensure_prototype(target_obj, stage: Usd.Stage, export_subtree_fn) -> Sdf.Path:
    """
    Create or retrieve a prototype for the target object.

    Args:
        target_obj: The FreeCAD object to create a prototype for
        stage: The USD stage
        export_subtree_fn: Callback function(target_obj, proto_xform, target_global)
                          that exports the object's subtree into the prototype

    Returns:
        Sdf.Path to the prototype prim
    """
    key = prototype_key(target_obj)

    # Return cached if exists
    if key in PROTOTYPE_CACHE:
        return PROTOTYPE_CACHE[key]

    # Create prototype container
    protos_root = _get_prototypes_root(stage)
    proto_name = "Proto_" + _make_usd_safe(getattr(target_obj, "Label", "") or target_obj.Name)
    proto_path = protos_root.GetPath().AppendChild(proto_name)

    # Check if already exists in stage (from previous session)
    if stage.GetPrimAtPath(proto_path):
        PROTOTYPE_CACHE[key] = proto_path
        return proto_path

    # Create prototype root prim
    proto_xf = UsdGeom.Xform.Define(stage, proto_path)
    proto_xf.ClearXformOpOrder()

    # Get target's global placement for proper coordinate space
    target_global = _get_global_placement(target_obj, FreeCAD.Placement())

    # Export the subtree via callback
    export_subtree_fn(target_obj, proto_xf, target_global)

    # Cache and return
    PROTOTYPE_CACHE[key] = proto_path
    return proto_path


def make_instance(prim, proto_path: Sdf.Path):
    """
    Make a prim an instance of a prototype.

    Args:
        prim: The UsdPrim to make an instance
        proto_path: Path to the prototype
    """
    prim.GetReferences().AddInternalReference(proto_path)
    prim.SetInstanceable(True)
