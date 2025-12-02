import sys, os
import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
# Temporary solution
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


import FreeCAD
from pxr import UsdGeom, Gf
import re

def make_usd_safe(name: str) -> str:
    name = name.strip() or "Object"
    # replace illegal chars with '_'
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    # USD prim names cannot start with a digit
    if name[0].isdigit():
        name = "_" + name
    return name


def export_object(obj, parent_xform, stage):
    # Debug info
    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{obj.Label}'  Name='{obj.Name}'\n"
    )

    shape = obj.Shape

    # Tessellate: returns (points, faces)
    pts, faces = shape.tessellate(0.1)  # tune tolerance later

    # Convert FreeCAD.Vector → (x, y, z)
    points = [(p.x, p.y, p.z) for p in pts]

    faceVertexIndices = []
    faceVertexCounts = []

    for f in faces:
        # f is a tuple of vertex indices like (i0, i1, i2)
        # Maybe need to reverse or adjust indices
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    usd_name = make_usd_safe(obj.Label or obj.Name)
    prim_path = parent_xform.GetPath().AppendChild(usd_name)

    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    # Placement (FreeCAD → USD transform)
    pl = obj.Placement
    pos = pl.Base
    q = pl.Rotation.Q  # (w, x, y, z)

    xform = UsdGeom.Xform(usd_mesh.GetPrim())
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[0], q[1], q[2], q[3]))


# def export_object(obj, parent_xform, stage):
#     # Print object name
#     FreeCAD.Console.PrintMessage(
#         f"[USD] Exporting: User Label='{obj.Label}',  Unique Name='{obj.Name}'\n"
#     )
#
#     shape = obj.Shape
#
#     # Tessellate to a mesh
#     mesh = shape.tessellate(0.1)  # linear deflection – tune this
#
#     points = []
#     faceVertexIndices = []
#     faceVertexCounts = []
#
#     vertex_index = 0
#     for tri_pts, tri_ids in zip(mesh[0], mesh[1]):
#         # tri_pts = (v0, v1, v2) – each is a FreeCAD.Vector
#         for v in tri_pts:
#             points.append((v.x, v.y, v.z))
#         faceVertexIndices.extend([vertex_index, vertex_index + 1, vertex_index + 2])
#         faceVertexCounts.append(3)
#         vertex_index += 3
#
#     prim_path = parent_xform.GetPath().AppendChild(obj.Name)
#     usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)
#
#     usd_mesh.CreatePointsAttr(points)
#     usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)
#     usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
#
#     # Placement → transform
#     pl = obj.Placement
#     pos = pl.Base
#     rot = pl.Rotation.Q  # quaternion (w, x, y, z)
#
#     xform = UsdGeom.Xform(usd_mesh.GetPrim())
#     xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
#     xform.AddOrientOp().Set(Gf.Quatd(rot[0], rot[1], rot[2], rot[3]))
