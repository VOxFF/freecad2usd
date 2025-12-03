import sys, os
import re

import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Gf


def export(objects, filename):
    if not objects:
        doc = FreeCAD.ActiveDocument
        objects = doc.Objects

    stage = Usd.Stage.CreateNew(filename)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)  # adjust if you want Y-up

    # Create a root Xform
    root = UsdGeom.Xform.Define(stage, "/Scene")

    for obj in objects:
        if not hasattr(obj, "Shape"):
            continue
        export_object(obj, root, stage)

    stage.GetRootLayer().Save()


def make_usd_safe(name: str) -> str:
    name = name.strip() or "Object"
    # replace illegal chars with '_'
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    # USD prim names cannot start with a digit
    if name[0].isdigit():
        name = "_" + name
    return name


def tessellated_mesh_to_usd(shape, stage, parent_xform, usd_name, tess_tol=0.1):
    """
    Tessellate a FreeCAD shape and convert it into a UsdGeom.Mesh
    under parent_xform. Returns the created UsdGeom.Mesh.

    shape: FreeCAD shape (e.g., obj.Shape)
    tess_tol: tessellation tolerance (same as used before in shape.tessellate)
    """
    # Tessellate: returns (points, faces)
    pts, faces = shape.tessellate(tess_tol)

    # Convert FreeCAD.Vector → (x, y, z)
    points = [(p.x, p.y, p.z) for p in pts]

    faceVertexIndices = []
    faceVertexCounts = []

    for f in faces:
        # f is a tuple of vertex indices like (i0, i1, i2, ...)
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    return usd_mesh


def export_object(obj, parent_xform, stage):
    # Debug info
    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{obj.Label}'  Name='{obj.Name}'\n"
    )

    shape = obj.Shape
    usd_name = make_usd_safe(obj.Label or obj.Name)

    # Build the USD mesh from the FreeCAD shape via tessellation
    usd_mesh = tessellated_mesh_to_usd(shape, stage, parent_xform, usd_name, tess_tol=0.1)

    # Placement (FreeCAD → USD transform)
    pl = obj.Placement
    pos = pl.Base
    q = pl.Rotation.Q  # (w, x, y, z)

    xform = UsdGeom.Xform(usd_mesh.GetPrim())
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[0], q[1], q[2], q[3]))
