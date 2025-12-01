# FreeCAD USD Exporter

FreeCAD-USD-Exporter is a simple plugin that lets you export FreeCAD models to
[Universal Scene Description (USD)](https://openusd.org/) files (`.usd`, `.usda`).

It is intended for workflows where you model in FreeCAD and then inspect,
render, or simulate the scene in tools like `usdview`, Omniverse, or other USD-based DCCs.

---

## Features

- Export the **active FreeCAD document** or **selected objects** to USD
- Supports ASCII (`.usda`) or binary (`.usd`) files (depending on your implementation)
- Keeps a simple hierarchical structure that mirrors FreeCAD objects

---

## Requirements

- **FreeCAD** 0.20+ (Python 3 builds)
- **Python USD bindings** (`pxr`):
```bash
python3 -c "from pxr import Usd, UsdGeom; print('USD ok')"
```
  
## Installation
1. Find your FreeCAD user Mod directory

FreeCAD looks for workbenches and plugins under a user-specific Mod directory:

Linux
```bash
~/.FreeCAD/Mod
```
or
```bash
~/.local/share/FreeCAD/Mod
```

macOS
```bash
~/Library/Preferences/FreeCAD/Mod
```

Windows
```bash
%APPDATA%\FreeCAD\Mod
```

You can also check Edit → Preferences → General → Application → Paths in FreeCAD.

2. Copy / clone the exporter

Create a folder for the exporter inside Mod, for example:
```bash
~/.FreeCAD/Mod/FreeCAD_USD_Exporter/
```

Put your plugin files there, e.g.:
```
FreeCAD_USD_Exporter/
  ├── Init.py
  ├── InitGui.py        # if you add toolbar / menu command
  ├── export_usd.py     # main exporter implementation
  └── README.md
```

If this repository is hosted on Git, you can clone directly:

```bash
cd ~/.FreeCAD/Mod
git clone https://github.com/<your-user>/freecad-usd-exporter.git FreeCAD_USD_Exporter
```

Restart FreeCAD after installing the plug-in.

## Usage

1. Open or create a document in FreeCAD.
2. Select the objects you want to export (or nothing to export the whole document).
3. Go to File → Export…
4. In the file type dropdown, choose “USD (*.usd *.usda)”.
5. Pick a file name (e.g. my_model.usda) and save.

## Load your USD

Open the resulting file in usdview:
```bash
usdview my_model.usda
```
