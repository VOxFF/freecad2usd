import sys, os
import re

import FreeCAD
import FreeCADGui
import math

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


def tessellated_mesh_with_normals_to_usd(
    shape,
    stage,
    parent_xform,
    usd_name,
    tess_tol=0.1,
    angle_threshold=30.0  # degrees
):
    """
    Tessellate a FreeCAD shape and convert it into a UsdGeom.Mesh with
    per-face-vertex normals based on a smoothing angle.

    shape:          FreeCAD shape (e.g., obj.Shape)
    tess_tol:       tessellation tolerance (same as used before in shape.tessellate)
    angle_threshold: faces sharing a vertex are smoothed together if the angle
                     between their normals is <= angle_threshold (degrees).
    """
    # Tessellate: returns (points, faces)
    pts, faces = shape.tessellate(tess_tol)  # pts: [FreeCAD.Vector], faces: [(i0, i1, i2, ...)]

    # Convert FreeCAD.Vector → (x, y, z)
    points = [(p.x, p.y, p.z) for p in pts]

    faceVertexIndices = []
    faceVertexCounts  = []

    for f in faces:
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    # --- Compute per-face normals
    face_normals = []
    for f in faces:
        if len(f) < 3:
            # degenerate
            face_normals.append(FreeCAD.Vector(0, 0, 0))
            continue

        v0 = pts[f[0]]
        v1 = pts[f[1]]
        v2 = pts[f[2]]

        n = (v1 - v0).cross(v2 - v0)
        if n.Length > 0:
            n.normalize()
        face_normals.append(n)

    # --- Build vertex → incident faces adjacency -
    vertex_faces = {i: [] for i in range(len(pts))}
    for fi, f in enumerate(faces):
        for vi in f:
            vertex_faces[vi].append(fi)

    # --- Compute face-vertex normals with angle-based smoothing
    cos_threshold = math.cos(math.radians(angle_threshold))

    face_vertex_normals = []  # one normal per entry in faceVertexIndices, same order

    for fi, f in enumerate(faces):
        n_face = face_normals[fi]

        # If face normal is degenerate, just push zero normals for its vertices
        if n_face.Length == 0:
            for _ in f:
                face_vertex_normals.append((0.0, 0.0, 0.0))
            continue

        for vi in f:
            accum = FreeCAD.Vector(0, 0, 0)

            # Consider all faces sharing this vertex
            for adj_fi in vertex_faces[vi]:
                n_adj = face_normals[adj_fi]
                if n_adj.Length == 0:
                    continue

                # dot(n_face, n_adj) since both are normalized
                dot = n_face.dot(n_adj)
                # If angle between them is <= angle_threshold, include in smoothing group
                if dot >= cos_threshold:
                    accum = accum.add(n_adj)

            # Fallback to face normal if something went wrong
            if accum.Length == 0:
                n = n_face
            else:
                n = accum
                n.normalize()

            face_vertex_normals.append((n.x, n.y, n.z))

    # --- Create the USD Mesh
    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    # --- Per-face-vertex normals
    normals_attr = usd_mesh.CreateNormalsAttr(face_vertex_normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    return usd_mesh


def export_object(obj, parent_xform, stage):
    # Debug info
    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{obj.Label}'  Name='{obj.Name}'\n"
    )

    shape = obj.Shape
    usd_name = make_usd_safe(obj.Label or obj.Name)

    # Build the USD mesh from the FreeCAD shape via tessellation
    #usd_mesh = tessellated_mesh_to_usd(shape, stage, parent_xform, usd_name, tess_tol=0.1)
    usd_mesh = tessellated_mesh_with_normals_to_usd(shape, stage, parent_xform, usd_name, tess_tol=0.1, angle_threshold=10.0)

    # Placement (FreeCAD → USD transform)
    pl = obj.Placement
    pos = pl.Base
    q = pl.Rotation.Q  # (w, x, y, z)

    xform = UsdGeom.Xform(usd_mesh.GetPrim())
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[0], q[1], q[2], q[3]))
