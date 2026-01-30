"""
Mesh geometry export to USD.
"""

import sys
import os
import math

import FreeCAD

# Ensure USD Python bindings (pxr) are visible
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import UsdGeom, UsdShade, Gf


def _compute_face_normals_from_points(points, faces):
    """
    Compute face normals from point positions and face indices.

    Args:
        points: List of FreeCAD.Vector
        faces: List of tuples of vertex indices

    Returns:
        List of FreeCAD.Vector (one per face)
    """
    face_normals = []
    for f in faces:
        if len(f) < 3:
            face_normals.append(FreeCAD.Vector(0, 0, 0))
            continue

        v0 = points[f[0]]
        v1 = points[f[1]]
        v2 = points[f[2]]

        n = (v1 - v0).cross(v2 - v0)
        if n.Length > 0:
            n.normalize()
        face_normals.append(n)

    return face_normals


def _compute_smoothed_normals(points, faces, face_normals, angle_threshold):
    """
    Compute per-face-vertex smoothed normals with angle threshold.

    Adjacent faces whose normals differ by more than angle_threshold degrees
    will not contribute to each other's vertex normals (hard edge).

    Args:
        points: List of points (for vertex count)
        faces: List of tuples of vertex indices
        face_normals: List of FreeCAD.Vector (one per face)
        angle_threshold: Degrees; faces differing by more than this create hard edges

    Returns:
        List of (x, y, z) tuples in face-varying order
    """
    # Build vertex -> incident faces mapping
    vertex_faces = {i: [] for i in range(len(points))}
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

    return face_vertex_normals


def _create_usd_mesh(stage, parent_xform, usd_name, points, faces, normals, material=None):
    """
    Create a UsdGeom.Mesh prim with the given geometry.

    Args:
        stage: USD stage
        parent_xform: Parent UsdGeom.Xform
        usd_name: Name for the mesh prim
        points: List of (x, y, z) tuples
        faces: List of tuples of vertex indices
        normals: List of (x, y, z) tuples in face-varying order
        material: Optional UsdShade.Material to bind

    Returns:
        UsdGeom.Mesh
    """
    face_vertex_indices = []
    face_vertex_counts = []
    for f in faces:
        face_vertex_indices.extend(f)
        face_vertex_counts.append(len(f))

    prim_path = parent_xform.GetPath().AppendChild(usd_name)
    usd_mesh = UsdGeom.Mesh.Define(stage, prim_path)

    usd_mesh.CreateSubdivisionSchemeAttr().Set("none")
    usd_mesh.CreateFaceVaryingLinearInterpolationAttr().Set("none")
    usd_mesh.CreateInterpolateBoundaryAttr().Set("none")

    usd_mesh.CreatePointsAttr(points)
    usd_mesh.CreateFaceVertexIndicesAttr(face_vertex_indices)
    usd_mesh.CreateFaceVertexCountsAttr(face_vertex_counts)

    usd_mesh.CreateNormalsAttr(normals)
    usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    if material is not None:
        UsdShade.MaterialBindingAPI.Apply(usd_mesh.GetPrim())
        UsdShade.MaterialBindingAPI(usd_mesh.GetPrim()).Bind(material)

    return usd_mesh


def _bbox(pts):
    """Compute bounding box from list of FreeCAD.Vector points."""
    if not pts:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    zs = [p.z for p in pts]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _pl_str(pl):
    """Format a FreeCAD Placement as a readable string."""
    try:
        b = pl.Base
        q = pl.Rotation.Q  # (x, y, z, w)
        return f"T=({b.x:.3f},{b.y:.3f},{b.z:.3f}) Qxyzw=({q[0]:.6f},{q[1]:.6f},{q[2]:.6f},{q[3]:.6f})"
    except Exception:
        return "<bad placement>"


def _transform_point_by_placement_inv(p, pl):
    """Apply inverse placement to a point (world -> local)."""
    inv = pl.inverse()
    return inv.multVec(p)


def tessellate_shape_to_usd(
    shape,
    stage,
    parent_xform,
    usd_name,
    tess_tolerance=0.1,
    angle_threshold=30.0,
    unbake_to_local=False,
    global_placement=None,
    material=None,
):
    """
    Tessellate a FreeCAD Shape and export as a USD Mesh.

    Args:
        shape: FreeCAD Shape to tessellate
        stage: USD stage
        parent_xform: Parent UsdGeom.Xform
        usd_name: Name for the mesh prim
        tess_tolerance: Tessellation tolerance (smaller = more triangles)
        angle_threshold: Degrees for normal smoothing hard edges
        unbake_to_local: If True, transform points to local space
        global_placement: Required if unbake_to_local is True
        material: Optional UsdShade.Material to bind

    Returns:
        UsdGeom.Mesh
    """
    pts, faces = shape.tessellate(tess_tolerance)

    bb0 = _bbox(pts)
    if bb0:
        FreeCAD.Console.PrintMessage(
            f"    [tess] '{usd_name}' raw bbox: "
            f"min=({bb0[0]:.3f},{bb0[1]:.3f},{bb0[2]:.3f}) "
            f"max=({bb0[3]:.3f},{bb0[4]:.3f},{bb0[5]:.3f})\n"
        )

    if unbake_to_local and global_placement is not None:
        pts_local = [_transform_point_by_placement_inv(p, global_placement) for p in pts]
        bb1 = _bbox(pts_local)
        if bb1:
            FreeCAD.Console.PrintMessage(
                f"    [tess] '{usd_name}' unbaked bbox: "
                f"min=({bb1[0]:.3f},{bb1[1]:.3f},{bb1[2]:.3f}) "
                f"max=({bb1[3]:.3f},{bb1[4]:.3f},{bb1[5]:.3f})\n"
            )
        FreeCAD.Console.PrintMessage(
            f"    [tess] '{usd_name}' unbake using global: {_pl_str(global_placement)}\n"
        )
        pts_for_normals = pts_local
        points = [(p.x, p.y, p.z) for p in pts_local]
    else:
        pts_for_normals = pts
        points = [(p.x, p.y, p.z) for p in pts]

    face_normals = _compute_face_normals_from_points(pts_for_normals, faces)
    normals = _compute_smoothed_normals(pts_for_normals, faces, face_normals, angle_threshold)

    return _create_usd_mesh(stage, parent_xform, usd_name, points, faces, normals, material)


def mesh_to_usd(
    mesh,
    stage,
    parent_xform,
    usd_name,
    angle_threshold=30.0,
    material=None,
):
    """
    Export a FreeCAD Mesh object to USD.

    Args:
        mesh: FreeCAD Mesh.Mesh object
        stage: USD stage
        parent_xform: Parent UsdGeom.Xform
        usd_name: Name for the mesh prim
        angle_threshold: Degrees for normal smoothing hard edges
        material: Optional UsdShade.Material to bind

    Returns:
        UsdGeom.Mesh
    """
    pts = mesh.Points
    facets = mesh.Facets

    points = [(p.x, p.y, p.z) for p in pts]

    # Convert 1-based indices to 0-based
    faces = [tuple(i - 1 for i in f.PointIndices) for f in facets]

    # Use facet normals directly
    face_normals = []
    for facet in facets:
        n = FreeCAD.Vector(facet.Normal)
        if n.Length > 0:
            n.normalize()
        face_normals.append(n)

    normals = _compute_smoothed_normals(pts, faces, face_normals, angle_threshold)

    return _create_usd_mesh(stage, parent_xform, usd_name, points, faces, normals, material)
