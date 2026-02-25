"""
USD export for FreeCAD TextBillboard objects (freecad_billboard plugin).

Exports billboard properties as billboard:* custom attributes on an Xform prim.
"""

import sys
import os

import FreeCAD

# Ensure USD Python bindings (pxr) are visible
usd_python_path = os.path.expanduser('~/usd_install/lib/python')
if usd_python_path not in sys.path:
    sys.path.append(usd_python_path)

from pxr import UsdGeom, Gf, Sdf


def is_text_billboard(obj) -> bool:
    """Check if obj is a TextBillboard from the freecad_billboard plugin."""
    proxy = getattr(obj, "Proxy", None)
    if proxy is not None and getattr(proxy, "Type", "") == "TextBillboard":
        return True
    # Property-based fallback (when Proxy is unavailable after document reload)
    return (
        hasattr(obj, "Text") and
        hasattr(obj, "FontSize") and
        hasattr(obj, "FontName") and
        hasattr(obj, "Alignment")
    )


def export_text_billboard(obj, xform: UsdGeom.Xform):
    """
    Write all TextBillboard properties as billboard:* custom attributes on the Xform prim.

    Args:
        obj: FreeCAD TextBillboard object
        xform: The UsdGeom.Xform prim already created for this object
    """
    prim = xform.GetPrim()

    if hasattr(obj, "Text"):
        prim.CreateAttribute("billboard:text", Sdf.ValueTypeNames.String).Set(str(obj.Text))
    if hasattr(obj, "FontSize"):
        prim.CreateAttribute("billboard:fontSize", Sdf.ValueTypeNames.Float).Set(float(obj.FontSize))
    if hasattr(obj, "FontName"):
        prim.CreateAttribute("billboard:fontName", Sdf.ValueTypeNames.String).Set(str(obj.FontName))
    if hasattr(obj, "TextColor"):
        prim.CreateAttribute("billboard:textColor", Sdf.ValueTypeNames.Color3f).Set(_color3f(obj.TextColor))
    if hasattr(obj, "Alignment"):
        prim.CreateAttribute("billboard:alignment", Sdf.ValueTypeNames.String).Set(str(obj.Alignment))

    if hasattr(obj, "ShowBackground"):
        prim.CreateAttribute("billboard:showBackground", Sdf.ValueTypeNames.Bool).Set(bool(obj.ShowBackground))
    if hasattr(obj, "BackgroundColor"):
        prim.CreateAttribute("billboard:backgroundColor", Sdf.ValueTypeNames.Color3f).Set(_color3f(obj.BackgroundColor))
    if hasattr(obj, "BackgroundPadding"):
        prim.CreateAttribute("billboard:backgroundPadding", Sdf.ValueTypeNames.Float).Set(float(obj.BackgroundPadding))

    if hasattr(obj, "ShowFrame"):
        prim.CreateAttribute("billboard:showFrame", Sdf.ValueTypeNames.Bool).Set(bool(obj.ShowFrame))
    if hasattr(obj, "FrameColor"):
        prim.CreateAttribute("billboard:frameColor", Sdf.ValueTypeNames.Color3f).Set(_color3f(obj.FrameColor))
    if hasattr(obj, "FrameWidth"):
        prim.CreateAttribute("billboard:frameWidth", Sdf.ValueTypeNames.Float).Set(float(obj.FrameWidth))

    FreeCAD.Console.PrintMessage(
        f"    [billboard] '{getattr(obj, 'Label', '')}' "
        f"text='{getattr(obj, 'Text', '')}' "
        f"fontSize={getattr(obj, 'FontSize', '')} "
        f"font='{getattr(obj, 'FontName', '')}'\n"
    )


def _color3f(color_prop) -> Gf.Vec3f:
    """Convert a FreeCAD color property (r, g, b, a) to Gf.Vec3f."""
    return Gf.Vec3f(float(color_prop[0]), float(color_prop[1]), float(color_prop[2]))
