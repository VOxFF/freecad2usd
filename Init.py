import FreeCAD

def register_usd_exporter():
    # UsdExporter.py is in the same folder; FreeCAD puts each Mod subdir on sys.path,
    # so "UsdExporter" is importable exactly as-is.
    FreeCAD.addExportType("USD (*.usd *.usda *.usdc)", "UsdExporter")


register_usd_exporter()
