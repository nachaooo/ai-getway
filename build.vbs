Set WshShell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)

WshShell.CurrentDirectory = scriptDir

' Check Python
CheckPython = WshShell.Run("cmd /c python --version >nul 2>nul", 0, True)
If CheckPython <> 0 Then
    MsgBox "Python not found. Please install Python 3.7+", vbCritical, "Build Failed"
    WScript.Quit 1
End If

' Check / Install PyInstaller
CheckPI = WshShell.Run("cmd /c pyinstaller --version >nul 2>nul", 0, True)
If CheckPI <> 0 Then
    MsgBox "Installing PyInstaller ...", vbInformation, "Building"
    WshShell.Run "cmd /c pip install pyinstaller", 1, True
End If

' Read and bump version using Python (preserves UTF-8 encoding)
Set sh = CreateObject("WScript.Shell")
Set exec = sh.Exec("python scripts\bump_version.py")
Do While exec.Status = 0
    WScript.Sleep 100
Loop
version = Trim(exec.StdOut.ReadAll())
If version = "" Then version = "0.0.0"

' Clean previous build cache
If fs.FolderExists(scriptDir & "\build") Then
    fs.DeleteFolder scriptDir & "\build", True
End If

' Run PyInstaller (use full path to avoid space-in-filename issues)
specPath = scriptDir & "\AI Gateway.spec"
exitCode = WshShell.Run("pyinstaller --clean """ & specPath & """", 1, True)

' Check result
distPath = scriptDir & "\dist"
If exitCode <> 0 Then
    MsgBox "Build failed (exit code: " & exitCode & "). Check console output for details.", vbCritical, "Build Failed"
    WScript.Quit 1
End If

If Not fs.FolderExists(distPath) Then
    MsgBox "Build failed: dist folder not found.", vbCritical, "Build Failed"
    WScript.Quit 1
End If

' Find latest AI Gateway exe in dist folder
latestExe = ""
latestTime = #1900-01-01#
Set folder = fs.GetFolder(distPath)
For Each file In folder.Files
    If InStr(file.Name, "AI Gateway v") > 0 And Right(file.Name, 4) = ".exe" Then
        If file.DateLastModified > latestTime Then
            latestTime = file.DateLastModified
            latestExe = file.Name
        End If
    End If
Next

If latestExe <> "" Then
    MsgBox "Build successful!" & vbCrLf & vbCrLf & "Output: " & latestExe & vbCrLf & "Path: " & distPath, _
           vbInformation, "Build Complete"
Else
    MsgBox "Build completed, but no AI Gateway exe found in dist folder.", vbExclamation, "Build Complete"
End If
