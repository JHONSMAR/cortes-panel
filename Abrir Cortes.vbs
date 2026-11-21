Option Explicit

Dim sh, fso, dir, tmp, f, outTxt
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' Verifica se o painel ja esta rodando na porta 8765 (sem janela de console)
tmp = sh.ExpandEnvironmentStrings("%TEMP%") & "\cortes_8765.txt"
sh.Run "cmd /c netstat -ano | findstr "":8765"" | findstr ""LISTENING"" > """ & tmp & """", 0, True
outTxt = ""
If fso.FileExists(tmp) Then
  Set f = fso.OpenTextFile(tmp, 1, False)
  If Not f.AtEndOfStream Then outTxt = f.ReadAll
  f.Close
  On Error Resume Next
  fso.DeleteFile tmp, True
  On Error GoTo 0
End If

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

' Abre o painel no navegador padrao
sh.Run "http://127.0.0.1:8765", 1, False
