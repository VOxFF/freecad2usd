import FreeCAD
import FreeCADGui
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


def export_object(obj, parent_xform, stage):
    shape = obj.Shape

    # Tessellate to a mesh
    mesh = shape.tessellate(0.1)  # linear deflection – tune this

    points = []
    faceVertexIndices = []
    faceVertexCounts = []

    vertex_index = 0
    for tri_pts, tri_ids in zip(mesh[0], mesh[1]):
        # tri_pts = (v0, v1, v2) – each is a FreeCAD.Vector
        for v in tri_pts:
            points.append((v.x, v.y, v.z))
        faceVertexIndices.extend([vertex_index, vertex_index + 1, vertex_index + 2])
        faceVertexCounts.append(3)
        vertex_index += 3

    prim_path = parent_xform.GetPath().AppendChild(obj.Name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)

    # Placement → transform
    pl = obj.Placement
    pos = pl.Base
    rot = pl.Rotation.Q  # quaternion (w, x, y, z)

    xform = UsdGeom.Xform(usd_mesh.GetPrim())
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatd(rot[0], rot[1], rot[2], rot[3]))
