Option Explicit

Dim sh, fso, dir, execObj, outTxt
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' Verifica se o painel j? est? rodando na porta 8765
Set execObj = sh.Exec("cmd /c netstat -ano | findstr "":8765"" | findstr ""LISTENING""")
outTxt = execObj.StdOut.ReadAll

If InStr(outTxt, "LISTENING") = 0 Then
  On Error Resume Next
  ' Inicia sem janela de console
  sh.Run "pythonw ""app.py"" --no-browser", 0, False
  If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python ""app.py"" --no-browser", 0, False
  End If
  On Error GoTo 0
  WScript.Sleep 1500
End If

' Abre o painel no navegador padr?o
sh.Run "http://127.0.0.1:8765", 1, False
