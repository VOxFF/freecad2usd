import sys, os
import re
import math

import FreeCAD
import FreeCADGui

# Ensure USD Python bindings (pxr) are visible to FreeCAD's Python
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import Usd, UsdGeom, Gf, Sdf


# If True, convert tessellated points into prim-local space by applying inverse(globalPlacement)
UNBAKE_POINTS_TO_LOCAL = True


# ----------------------------
# Link prototypes cache
# ----------------------------
PROTOTYPE_CACHE = {}  # key: (docName, targetName) -> Sdf.Path
PROTOTYPES_ROOT_PATH = Sdf.Path("/Scene/__Prototypes")


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


# ----------------------------
# Export entry
# ----------------------------

def export(objects, filename):
    if not objects:
        doc = FreeCAD.ActiveDocument
        objects = doc.Objects

    stage = Usd.Stage.CreateNew(filename)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    # Optional: FreeCAD is typically mm.
    # UsdGeom.SetStageMetersPerUnit(stage, 0.001)

    root = UsdGeom.Xform.Define(stage, "/Scene")

    identity = FreeCAD.Placement()
    for obj in objects:
        export_object_recursive(obj, root, stage, parent_global=identity, is_prototype_root=False)

    stage.GetRootLayer().Save()


# ----------------------------
# Utils
# ----------------------------

def make_usd_safe(name: str) -> str:
    name = (name or "").strip() or "Object"
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name[0].isdigit():
        name = "_" + name
    return name


def get_children(obj):
    """
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


# ----------------------------
# Link + prototypes helpers
# ----------------------------

def _get_linked_object(obj):
    # Standard FreeCAD Link API
    if hasattr(obj, "LinkedObject"):
        return obj.LinkedObject
    # Some variants may expose 'Link'
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
    return UsdGeom.Xform.Define(stage, str(PROTOTYPES_ROOT_PATH))


def _prototype_key_for_target(target_obj):
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

    proto_name = "Proto_" + make_usd_safe(getattr(target_obj, "Label", "") or target_obj.Name)
    proto_path = protos_root.GetPath().AppendChild(proto_name)

    # If already exists in stage, reuse
    if stage.GetPrimAtPath(proto_path):
        PROTOTYPE_CACHE[key] = proto_path
        return proto_path

    # Create prototype root prim (identity)
    proto_xf = UsdGeom.Xform.Define(stage, proto_path)
    proto_xf.ClearXformOpOrder()

    # Baseline = target global placement so target local becomes identity in prototype
    target_global = get_global_placement(target_obj, FreeCAD.Placement())

    export_object_recursive(
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
    We'll treat wrapper itself as the instance node and avoid nested link prim hierarchy.
    """
    kids = []
    if hasattr(obj, "Group") and obj.Group:
        kids.extend(obj.Group)
    if hasattr(obj, "OutList") and obj.OutList:
        kids.extend(obj.OutList)

    uniq = []
    seen = set()
    for k in kids:
        if id(k) not in seen:
            uniq.append(k)
            seen.add(id(k))

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

        # list/tuple
        if isinstance(v, (list, tuple)):
            if len(v) == 0:
                continue
            v0 = v[0]
            # sometimes stored as (obj, subname)
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

def export_object_recursive(obj, parent_xform, stage, parent_global: FreeCAD.Placement, is_prototype_root=False):
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

    if is_prototype_root:
        # Prototype root: force identity local transform
        local_to_parent = FreeCAD.Placement()
    else:
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
    FreeCAD.Console.PrintMessage(f"    USD xform authored: {_pl_str(local_to_parent)}\n")

    # ------------------------------------------------------------------
    # 1) Clone-of-link wrapper (single App::Link child) -> instance here
    # ------------------------------------------------------------------
    if not is_prototype_root:
        link_child = _get_single_link_child(obj)
        if link_child is not None and not type_id.startswith("App::Link"):
            target = _resolve_link_chain(link_child)
            if target is None:
                FreeCAD.Console.PrintMessage("  -> Link-wrapper has broken target, skipping\n")
                return

            proto_path = _ensure_prototype_for_target(target, stage)
            prim = this_xform.GetPrim()
            prim.GetReferences().AddInternalReference(proto_path)
            prim.SetInstanceable(True)
            return

    # ------------------------------------------------------------------
    # 2) FeaturePython clone that ultimately points to an App::Link
    #    (this fixes your 'Clone005' case)
    # ------------------------------------------------------------------
    if type_id.startswith("Part::FeaturePython") and not is_prototype_root:
        link_obj = _resolve_to_link_if_any(obj)
        if link_obj is not None:
            target = _resolve_link_chain(link_obj)
            if target is None:
                FreeCAD.Console.PrintMessage("  -> Clone-of-link has broken target, skipping\n")
                return

            proto_path = _ensure_prototype_for_target(target, stage)
            prim = this_xform.GetPrim()
            prim.GetReferences().AddInternalReference(proto_path)
            prim.SetInstanceable(True)

            # Do NOT export baked Shape and do NOT recurse.
            return

    # ------------------------------------------------------------------
    # 3) App::Link itself -> instance of prototype
    # ------------------------------------------------------------------
    if type_id.startswith("App::Link") and not is_prototype_root:
        target = _resolve_link_chain(obj)
        if target is None:
            FreeCAD.Console.PrintMessage("  -> App::Link has broken target, skipping\n")
            return

        proto_path = _ensure_prototype_for_target(target, stage)
        prim = this_xform.GetPrim()
        prim.GetReferences().AddInternalReference(proto_path)
        prim.SetInstanceable(True)
        return

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
        export_object_recursive(child, this_xform, stage, parent_global=child_global, is_prototype_root=False)


# ----------------------------
# Mesh export helpers
# ----------------------------

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
    if bb0:
        FreeCAD.Console.PrintMessage(
            f"    [tess] '{usd_name}' raw bbox: "
            f"min=({bb0[0]:.3f},{bb0[1]:.3f},{bb0[2]:.3f}) "
            f"max=({bb0[3]:.3f},{bb0[4]:.3f},{bb0[5]:.3f})\n"
        )

    if unbake_points_to_local and unbake_using_global_placement is not None:
        pts_local = [transform_point_by_placement_inv(p, unbake_using_global_placement) for p in pts]
        bb1 = _bbox(pts_local)
        if bb1:
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
