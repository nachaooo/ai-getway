Set WshShell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)

WshShell.CurrentDirectory = scriptDir

CheckResult = WshShell.Run("cmd /c ""python -c ""import pystray; import PIL"" 2>nul""", 0, True)

If CheckResult = 0 Then
    WshShell.Run "cmd /c python tray.py", 0, False
Else
    WshShell.Run "cmd /c ""pip install -q pystray Pillow && python tray.py""", 0, False
End If
