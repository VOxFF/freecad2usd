import sys, os
import re
import math

import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Gf


# If True, convert tessellated points into prim-local space by applying inverse(globalPlacement)
UNBAKE_POINTS_TO_LOCAL = True


# ----------------------------
# Debug helpers
# ----------------------------

def _quat_xyzw(pl: FreeCAD.Placement):
    """
    FreeCAD Rotation.Q is (x, y, z, w) (NOT w,x,y,z).
    """
    q = pl.Rotation.Q
    return (q[0], q[1], q[2], q[3])  # x,y,z,w


def _pl_str(pl):
    try:
        b = pl.Base
        x, y, z, w = _quat_xyzw(pl)
        return f"T=({b.x:.3f},{b.y:.3f},{b.z:.3f}) Qxyzw=({x:.6f},{y:.6f},{z:.6f},{w:.6f})"
    except Exception:
        return "<bad placement>"


def _bbox(pts_):
    if not pts_:
        return None
    xs = [p.x for p in pts_]
    ys = [p.y for p in pts_]
    zs = [p.z for p in pts_]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def export(objects, filename):
    if not objects:
        doc = FreeCAD.ActiveDocument
        objects = doc.Objects

    stage = Usd.Stage.CreateNew(filename)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    # Optional: FreeCAD is typically mm. Enable later when transforms are correct.
    # UsdGeom.SetStageMetersPerUnit(stage, 0.001)

    root = UsdGeom.Xform.Define(stage, "/Scene")

    identity = FreeCAD.Placement()
    for obj in objects:
        export_object_recursive(obj, root, stage, parent_global=identity)

    stage.GetRootLayer().Save()


def make_usd_safe(name: str) -> str:
    name = (name or "").strip() or "Object"
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name[0].isdigit():
        name = "_" + name
    return name


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


def get_global_placement(obj, parent_global: FreeCAD.Placement):
    """
    Prefer FreeCAD's global placement if available. Otherwise, approximate by chaining.
    """
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


def placement_inverse(pl: FreeCAD.Placement) -> FreeCAD.Placement:
    return pl.inverse()


def placement_to_usd_ops(xform: UsdGeom.Xform, pl: FreeCAD.Placement):
    """
    Author local transform for this prim (relative to parent) using T + Orient.
    IMPORTANT: FreeCAD quaternion is (x,y,z,w). USD expects (w, x, y, z) for Gf.Quat*.
    """
    pos = pl.Base
    x, y, z, w = _quat_xyzw(pl)

    xform.ClearXformOpOrder()
    xform.AddTranslateOp().Set(Gf.Vec3d(pos.x, pos.y, pos.z))
    xform.AddOrientOp().Set(Gf.Quatf(w, x, y, z))


def transform_point_by_placement_inv(p: FreeCAD.Vector, pl: FreeCAD.Placement) -> FreeCAD.Vector:
    """
    Apply inverse placement to a point (world -> local).
    """
    inv = pl.inverse()
    return inv.multVec(p)


def export_object_recursive(obj, parent_xform, stage, parent_global: FreeCAD.Placement):
    type_id = getattr(obj, "TypeId", "")
    label = getattr(obj, "Label", "") or getattr(obj, "Name", "")
    usd_name = make_usd_safe(label)

    FreeCAD.Console.PrintMessage(
        f"[USD] Exporting: Label='{label}'  Name='{getattr(obj,'Name','')}'  TypeId='{type_id}'\n"
    )

    # Create Xform for this object under its parent
    this_path = parent_xform.GetPath().AppendChild(usd_name)
    this_xform = UsdGeom.Xform.Define(stage, this_path)

    # Compute placements
    child_global = get_global_placement(obj, parent_global)
    parent_global_inv = placement_inverse(parent_global)
    local_to_parent = parent_global_inv.multiply(child_global)

    # DEBUG: placements + embedded shape placement
    FreeCAD.Console.PrintMessage(
        f"    parent_global   : {_pl_str(parent_global)}\n"
        f"    child_global    : {_pl_str(child_global)}\n"
        f"    local_to_parent : {_pl_str(local_to_parent)}\n"
    )
    if hasattr(obj, "Shape") and getattr(obj, "Shape", None) and not obj.Shape.isNull():
        try:
            FreeCAD.Console.PrintMessage(f"    shape.Placement : {_pl_str(obj.Shape.Placement)}\n")
        except Exception:
            FreeCAD.Console.PrintMessage("    shape.Placement : <unavailable>\n")

    placement_to_usd_ops(this_xform, local_to_parent)

    # DEBUG: what we authored to USD (show as xyzw so it matches FreeCAD print)
    FreeCAD.Console.PrintMessage(f"    USD xform authored: {_pl_str(local_to_parent)}\n")

    # -------------------------
    # Containers only
    # -------------------------
    if type_id.startswith("App::Part"):
        FreeCAD.Console.PrintMessage("  -> App::Part, container only\n")

    elif ("DocumentObjectGroup" in type_id) or ("GeoFeatureGroup" in type_id) or (
        hasattr(obj, "Group")
        and getattr(obj, "Group")
        and not hasattr(obj, "Shape")
        and not hasattr(obj, "Mesh")
    ):
        FreeCAD.Console.PrintMessage("  -> Group, container only\n")

    # -------------------------
    # PartDesign::Body (one mesh, no recursion into features)
    # -------------------------
    elif type_id == "PartDesign::Body":
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            FreeCAD.Console.PrintMessage("  -> PartDesign::Body, exporting Body.Shape as mesh\n")
            mesh_name = usd_name + "_mesh"

            tessellated_mesh_with_normals_to_usd(
                obj.Shape,
                stage,
                this_xform,
                mesh_name,
                tess_tol=0.1,
                angle_threshold=10.0,
                unbake_points_to_local=UNBAKE_POINTS_TO_LOCAL,
                unbake_using_global_placement=child_global,
            )
        else:
            FreeCAD.Console.PrintMessage("  -> PartDesign::Body has no valid Shape\n")
        return

    # -------------------------
    # Mesh::Feature (use existing triangulation)
    # -------------------------
    elif type_id.startswith("Mesh::Feature"):
        if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None) and obj.Mesh.Facets:
            FreeCAD.Console.PrintMessage("  -> Mesh::Feature, exporting original Mesh\n")
            mesh_name = usd_name + "_mesh"

            original_mesh_with_normals_to_usd(
                obj.Mesh,
                stage,
                this_xform,
                mesh_name,
                angle_threshold=10.0
            )
        else:
            FreeCAD.Console.PrintMessage("  -> Mesh::Feature has no facets\n")

    # -------------------------
    # Part::Feature (tessellate)
    # -------------------------
    elif type_id.startswith("Part::Feature"):
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            FreeCAD.Console.PrintMessage("  -> Part::Feature, exporting tessellated Shape\n")
            mesh_name = usd_name + "_mesh"

            tessellated_mesh_with_normals_to_usd(
                obj.Shape,
                stage,
                this_xform,
                mesh_name,
                tess_tol=0.1,
                angle_threshold=10.0,
                unbake_points_to_local=UNBAKE_POINTS_TO_LOCAL,
                unbake_using_global_placement=child_global,
            )
        else:
            FreeCAD.Console.PrintMessage("  -> Part::Feature has no valid Shape\n")

    # -------------------------
    # Fallback
    # -------------------------
    else:
        if hasattr(obj, "Mesh") and getattr(obj.Mesh, "Facets", None) and obj.Mesh.Facets:
            FreeCAD.Console.PrintMessage("  -> unknown type, exporting Mesh property\n")
            mesh_name = usd_name + "_mesh"
            original_mesh_with_normals_to_usd(
                obj.Mesh,
                stage,
                this_xform,
                mesh_name,
                angle_threshold=10.0
            )
        elif hasattr(obj, "Shape") and not obj.Shape.isNull():
            FreeCAD.Console.PrintMessage("  -> unknown type, exporting Shape via tessellation\n")
            mesh_name = usd_name + "_mesh"
            tessellated_mesh_with_normals_to_usd(
                obj.Shape,
                stage,
                this_xform,
                mesh_name,
                tess_tol=0.1,
                angle_threshold=10.0,
                unbake_points_to_local=UNBAKE_POINTS_TO_LOCAL,
                unbake_using_global_placement=child_global,
            )
        else:
            FreeCAD.Console.PrintMessage("  -> no Mesh/Shape, container only\n")

    # recurse into children
    for child in get_children(obj):
        vo = getattr(child, "ViewObject", None)
        if vo and not vo.Visibility:
            continue
        export_object_recursive(child, this_xform, stage, parent_global=child_global)


def tessellated_mesh_with_normals_to_usd(
    shape,
    stage,
    parent_xform,
    usd_name,
    tess_tol=0.1,
    angle_threshold=30.0,
    unbake_points_to_local=False,
    unbake_using_global_placement=None,
):
    pts, faces = shape.tessellate(tess_tol)

    bb0 = _bbox(pts)
    FreeCAD.Console.PrintMessage(
        f"    [tess] '{usd_name}' raw bbox: "
        f"min=({bb0[0]:.3f},{bb0[1]:.3f},{bb0[2]:.3f}) "
        f"max=({bb0[3]:.3f},{bb0[4]:.3f},{bb0[5]:.3f})\n"
    )

    if unbake_points_to_local and unbake_using_global_placement is not None:
        pts_local = [transform_point_by_placement_inv(p, unbake_using_global_placement) for p in pts]
        bb1 = _bbox(pts_local)
        FreeCAD.Console.PrintMessage(
            f"    [tess] '{usd_name}' unbaked bbox: "
            f"min=({bb1[0]:.3f},{bb1[1]:.3f},{bb1[2]:.3f}) "
            f"max=({bb1[3]:.3f},{bb1[4]:.3f},{bb1[5]:.3f})\n"
        )
        FreeCAD.Console.PrintMessage(
            f"    [tess] '{usd_name}' unbake using global: {_pl_str(unbake_using_global_placement)}\n"
        )

        points = [(p.x, p.y, p.z) for p in pts_local]
        pts_for_normals = pts_local
    else:
        points = [(p.x, p.y, p.z) for p in pts]
        pts_for_normals = pts

    faceVertexIndices = []
    faceVertexCounts = []
    for f in faces:
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    # Face normals
    face_normals = []
    for f in faces:
        if len(f) < 3:
            face_normals.append(FreeCAD.Vector(0, 0, 0))
            continue

        v0 = pts_for_normals[f[0]]
        v1 = pts_for_normals[f[1]]
        v2 = pts_for_normals[f[2]]

        n = (v1 - v0).cross(v2 - v0)
        if n.Length > 0:
            n.normalize()
        face_normals.append(n)

    # Vertex -> incident faces
    vertex_faces = {i: [] for i in range(len(pts_for_normals))}
    for fi, f in enumerate(faces):
        for vi in f:
            vertex_faces[vi].append(fi)

    cos_threshold = math.cos(math.radians(angle_threshold))
    face_vertex_normals = []

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
                if n_face.dot(n_adj) >= cos_threshold:
                    accum = accum.add(n_adj)

            if accum.Length == 0:
                n = FreeCAD.Vector(n_face)
            else:
                n = accum
                n.normalize()

            face_vertex_normals.append((n.x, n.y, n.z))

    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    usd_mesh.CreateNormalsAttr(face_vertex_normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    return usd_mesh


def original_mesh_with_normals_to_usd(
    mesh,
    stage,
    parent_xform,
    usd_name,
    angle_threshold=30.0
):
    pts = mesh.Points
    facets = mesh.Facets

    points = [(p.x, p.y, p.z) for p in pts]

    faces = []
    for f in facets:
        faces.append(tuple(i - 1 for i in f.PointIndices))  # 1-based -> 0-based

    faceVertexIndices = []
    faceVertexCounts = []
    for f in faces:
        faceVertexIndices.extend(f)
        faceVertexCounts.append(len(f))

    face_normals = []
    for facet in facets:
        n = FreeCAD.Vector(facet.Normal)
        if n.Length > 0:
            n.normalize()
        face_normals.append(n)

    vertex_faces = {i: [] for i in range(len(pts))}
    for fi, f in enumerate(faces):
        for vi in f:
            vertex_faces[vi].append(fi)

    cos_threshold = math.cos(math.radians(angle_threshold))
    face_vertex_normals = []

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
                if n_face.dot(n_adj) >= cos_threshold:
                    accum = accum.add(n_adj)

            if accum.Length == 0:
                n = FreeCAD.Vector(n_face)
            else:
                n = accum
                n.normalize()

            face_vertex_normals.append((n.x, n.y, n.z))

    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(faceVertexIndices)
    usd_mesh.CreateFaceVertexCountsAttr(faceVertexCounts)

    usd_mesh.CreateNormalsAttr(face_vertex_normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    return usd_mesh
