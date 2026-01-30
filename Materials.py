"""
USD Material creation and FreeCAD material property extraction.
"""

import sys
import os

import FreeCAD

# Ensure USD Python bindings (pxr) are visible
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import UsdGeom, UsdShade, Gf, Sdf


MATERIAL_CACHE = {}   # key: (r, g, b, sr, sg, sb, roughness, opacity) -> Sdf.Path
MATERIALS_ROOT_PATH = Sdf.Path("/Scene/__Materials")


def clear_cache():
    """Clear the material cache. Call at the start of each export."""
    MATERIAL_CACHE.clear()


def _get_materials_root(stage):
    """Create or get the hidden __Materials container."""
    xf = UsdGeom.Xform.Define(stage, str(MATERIALS_ROOT_PATH))
    UsdGeom.Imageable(xf.GetPrim()).GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    xf.GetPrim().SetMetadata("hidden", True)
    return xf


def extract_material_properties(obj):
    """
    Read diffuse color, specular color, roughness, and opacity from a FreeCAD object's ViewObject.

    Returns a dict with keys: diffuse, specular, roughness, opacity
    Returns None if no color data is available.
    """
    vo = getattr(obj, "ViewObject", None)
    if vo is None:
        return None

    # Diffuse color: try DiffuseColor list first, fall back to ShapeColor
    diffuse = None
    dc = getattr(vo, "DiffuseColor", None)
    if dc and isinstance(dc, (list, tuple)) and len(dc) > 0:
        # DiffuseColor is a list of (r, g, b, a) per face; take the first
        c = dc[0]
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            diffuse = (float(c[0]), float(c[1]), float(c[2]))

    if diffuse is None:
        sc = getattr(vo, "ShapeColor", None)
        if sc and isinstance(sc, (list, tuple)) and len(sc) >= 3:
            diffuse = (float(sc[0]), float(sc[1]), float(sc[2]))

    if diffuse is None:
        return None

    # Opacity from Transparency (0-100 integer -> 0.0-1.0 opacity)
    transparency = getattr(vo, "Transparency", 0)
    if isinstance(transparency, (int, float)):
        opacity = 1.0 - (float(transparency) / 100.0)
    else:
        opacity = 1.0

    # Specular + shininess from ShapeMaterial if available
    specular = (0.5, 0.5, 0.5)
    roughness = 0.5
    mat = getattr(vo, "ShapeMaterial", None)
    if mat is not None:
        spec = getattr(mat, "SpecularColor", None)
        if spec and hasattr(spec, "r"):
            specular = (float(spec.r), float(spec.g), float(spec.b))
        elif isinstance(spec, (list, tuple)) and len(spec) >= 3:
            specular = (float(spec[0]), float(spec[1]), float(spec[2]))

        shininess = getattr(mat, "Shininess", None)
        if isinstance(shininess, (int, float)):
            roughness = 1.0 - max(0.0, min(1.0, float(shininess)))

    return {
        "diffuse": diffuse,
        "specular": specular,
        "roughness": round(roughness, 4),
        "opacity": round(opacity, 4),
    }


def _material_cache_key(props):
    """Create a cache key from material properties, rounded for deduplication."""
    d = props["diffuse"]
    s = props["specular"]
    return (
        round(d[0], 3), round(d[1], 3), round(d[2], 3),
        round(s[0], 3), round(s[1], 3), round(s[2], 3),
        round(props["roughness"], 3),
        round(props["opacity"], 3),
    )


def ensure_material(stage, props):
    """
    Create or reuse a UsdPreviewSurface material under /Scene/__Materials.

    Args:
        stage: The USD stage
        props: Dict from extract_material_properties()

    Returns:
        UsdShade.Material
    """
    key = _material_cache_key(props)
    if key in MATERIAL_CACHE:
        return UsdShade.Material.Get(stage, MATERIAL_CACHE[key])

    mats_root = _get_materials_root(stage)

    # Name based on diffuse color to keep it readable
    d = props["diffuse"]
    mat_name = "Mat_{:02X}{:02X}{:02X}".format(
        int(d[0] * 255), int(d[1] * 255), int(d[2] * 255)
    )

    # Disambiguate if needed (different specular/roughness/opacity for same diffuse)
    mat_path = mats_root.GetPath().AppendChild(mat_name)
    if stage.GetPrimAtPath(mat_path) and key not in MATERIAL_CACHE:
        suffix = 1
        while stage.GetPrimAtPath(mats_root.GetPath().AppendChild(mat_name + f"_{suffix}")):
            suffix += 1
        mat_name = mat_name + f"_{suffix}"
        mat_path = mats_root.GetPath().AppendChild(mat_name)

    material = UsdShade.Material.Define(stage, mat_path)

    # Create UsdPreviewSurface shader
    shader_path = mat_path.AppendChild("PreviewSurface")
    shader = UsdShade.Shader.Define(stage, shader_path)
    shader.CreateIdAttr("UsdPreviewSurface")

    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(*props["diffuse"])
    )
    shader.CreateInput("specularColor", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(*props["specular"])
    )
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(props["roughness"])
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(props["opacity"])
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)

    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    MATERIAL_CACHE[key] = mat_path
    FreeCAD.Console.PrintMessage(
        f"    [mat] Created material '{mat_name}' diffuse={props['diffuse']} "
        f"spec={props['specular']} rough={props['roughness']} opacity={props['opacity']}\n"
    )
    return material
