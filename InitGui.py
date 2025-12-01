
import FreeCADGui

class USDWorkbench(FreeCADGui.Workbench):
    MenuText = "USD"
    ToolTip = "USD Export tools"
    Icon = ""

    def Initialize(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"

FreeCADGui.addWorkbench(USDWorkbench())
