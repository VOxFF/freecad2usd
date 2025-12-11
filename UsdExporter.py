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
        export_object_recursive(obj, root, stage)
        # if hasattr(obj, "Shape") or hasattr(obj, "Mesh"):
        #     export_object(obj, root, stage)


    stage.GetRootLayer().Save()


def export_object(obj, parent_xform, stage):
    # Debug info
    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{obj.Label}'  Name='{obj.Name}'\n"
    )

    usd_name = make_usd_safe(obj.Label or obj.Name)
    if hasattr(obj, "Mesh") and obj.Mesh.Facets:
        FreeCAD.Console.PrintMessage("as Mesh")
        usd_mesh = original_mesh_with_normals_to_usd(obj.Mesh, stage, parent_xform, usd_name,angle_threshold=10.0)

    elif hasattr(obj, "Shape") and not obj.Shape.isNull():
        # Build the USD mesh from the FreeCAD shape via tessellation
        FreeCAD.Console.PrintMessage("as tessellated Shape")
        usd_mesh = tessellated_mesh_with_normals_to_usd(obj.Shape, stage, parent_xform, usd_name, tess_tol=0.1, angle_threshold=10.0)

    else:
        FreeCAD.Console.PrintMessage("Skipping: no mesh or shape")

    # Placement (FreeCAD → USD transform)
    pl = obj.Placement
    pos = pl.Base
    q = pl.Rotation.Q  # (w, x, y, z)

    xform = UsdGeom.Xform(usd_mesh.GetPrim())
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(q[0], q[1], q[2], q[3]))


def export_object_recursive(obj, parent_xform, stage):
    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting object: Label='{obj.Label}'  Name='{obj.Name}'\n"
    )

    usd_name = make_usd_safe(obj.Label or obj.Name)

    # Create USD Xform node for this object
    this_path = parent_xform.GetPath().AppendChild(usd_name)
    this_xform = UsdGeom.Xform.Define(stage, this_path)

    # Apply object local transform
    if hasattr(obj, "Placement"):
        pl = obj.Placement
        pos = pl.Base
        q = pl.Rotation.Q  # (w, x, y, z)
        this_xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
        this_xform.AddOrientOp().Set(Gf.Quatf(q[0], q[1], q[2], q[3]))

    # Export geometry if present
    mesh_name = usd_name + "_mesh"

    if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None):
        if obj.Mesh.Facets:
            FreeCAD.Console.PrintMessage("as Mesh\n")
            original_mesh_with_normals_to_usd(
                obj.Mesh,
                stage,
                this_xform,
                mesh_name,
                angle_threshold=10.0
            )

    elif hasattr(obj, "Shape") and not obj.Shape.isNull():
        FreeCAD.Console.PrintMessage("as tessellated Shape\n")
        tessellated_mesh_with_normals_to_usd(
            obj.Shape,
            stage,
            this_xform,
            mesh_name,
            tess_tol=0.1,
            angle_threshold=10.0
        )

    else:
        FreeCAD.Console.PrintMessage("Skipping: no mesh or shape\n")

    # Recurse into children
    for child in get_children(obj):
        vo = getattr(child, "ViewObject", None)
        if vo and not vo.Visibility:
            continue
        export_object_recursive(child, this_xform, stage)

def get_children(obj):
    children = []
    seen = set()

    if hasattr(obj, "Group") and obj.Group:
        for ch in obj.Group:
            if ch not in seen:
                children.append(ch)
                seen.add(ch)

    if hasattr(obj, "OutList"):
        for ch in obj.OutList:
            if ch not in seen:
                children.append(ch)
                seen.add(ch)

    return children


"""
This functon is used for:
- The object comes from Part or PartDesign workbench
- The object has a .Shape attribute (exact B-Rep geometry)
- You need to generate a mesh from analytic surfaces via shape.tessellate()
- You want control over tessellation tolerance (mesh resolution)
- You want normals computed from smooth CAD surfaces

This is required for:
- Solids, extrusions, revolves, lofts, fillets, chamfers
- Any parametric CAD geometry in FreeCAD

Because these objects have no .Mesh, you MUST tessellate first.
"""
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

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

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

    # Compute per-face normals
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

    # Build vertex → incident faces adjacency -
    vertex_faces = {i: [] for i in range(len(pts))}
    for fi, f in enumerate(faces):
        for vi in f:
            vertex_faces[vi].append(fi)

    # Compute face-vertex normals with angle-based smoothing
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

    # Create the USD Mesh
    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    # Per-face-vertex normals
    normals_attr = usd_mesh.CreateNormalsAttr(face_vertex_normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    return usd_mesh

"""
This functon is used for:
- The FreeCAD object is a real Mesh object (Mesh::Feature)
- The object was imported from STL/OBJ
- The object was created in the Mesh Workbench
- The geometry is already triangulated and has no parametric surfaces
- You want to keep the original triangle structure exactly as-is
"""
def original_mesh_with_normals_to_usd(
    mesh,              # FreeCAD Mesh.Mesh (e.g. obj.Mesh)
    stage,
    parent_xform,
    usd_name,
    angle_threshold=30.0  # degrees
):
    """
    Export a FreeCAD Mesh.Mesh as a UsdGeom.Mesh with per-face-vertex normals.

    - Uses the mesh's existing triangulation (no tessellate()).
    - Builds face-varying normals with an angle-based smoothing threshold.
    """

    #  Collect points and faces from FreeCAD mesh
    # Points are 1-based in FreeCAD mesh; convert to 0-based.
    pts = mesh.Points
    facets = mesh.Facets

    points = [(p.x, p.y, p.z) for p in pts]

    faces = []
    for f in facets:
        # f.PointIndices is a tuple of 1-based indices (i1, i2, i3, ...)
        idxs = tuple(i - 1 for i in f.PointIndices)
        faces.append(idxs)

    faceVertexIndices = []
    faceVertexCounts = []

    for f in faces:
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    # Face normals
    face_normals = []
    for f, facet in zip(faces, facets):
        # FreeCAD facet.Normal is already a Vector
        n = facet.Normal
        if n.Length > 0:
            n = n.normalize() if hasattr(n, "normalize") else n
        face_normals.append(FreeCAD.Vector(n))

    # Vertex → incident faces adjacency
    vertex_faces = {i: [] for i in range(len(pts))}
    for fi, f in enumerate(faces):
        for vi in f:
            vertex_faces[vi].append(fi)

    # Face-vertex normals with angle threshold
    cos_threshold = math.cos(math.radians(angle_threshold))
    face_vertex_normals = []  # 1:1 with faceVertexIndices

    for fi, f in enumerate(faces):
        n_face = face_normals[fi]

        if n_face.Length == 0:
            for _ in f:
                face_vertex_normals.append((0.0, 0.0, 0.0))
            continue

        for vi in f:
            accum = FreeCAD.Vector(0, 0, 0)

            for adj_fi in vertex_faces[vi]:
                n_adj = face_normals[adj_fi]
                if n_adj.Length == 0:
                    continue

                dot = n_face.dot(n_adj)
                if dot >= cos_threshold:  # angle <= threshold
                    accum = accum.add(n_adj)

            if accum.Length == 0:
                n = n_face
            else:
                n = accum
                n.normalize()

            face_vertex_normals.append((n.x, n.y, n.z))

    # Create USD mesh
    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    # Force polygonal interpretation to use our normals
    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    normals_attr = usd_mesh.CreateNormalsAttr(face_vertex_normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    return usd_mesh



def make_usd_safe(name: str) -> str:
    name = name.strip() or "Object"
    # replace illegal chars with '_'
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    # USD prim names cannot start with a digit
    if name[0].isdigit():
        name = "_" + name
    return name