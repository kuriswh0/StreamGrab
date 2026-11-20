; StreamGrab.exe: tiny launcher that starts the bundled Python engine without a console window.
; Built by windows/build.sh (makensis -DOUT=... -DICON=... -DVERSION=...).
Unicode true
Name "StreamGrab"
OutFile "${OUT}"
Icon "${ICON}"
SilentInstall silent
RequestExecutionLevel user
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "StreamGrab"
VIAddVersionKey "FileDescription" "StreamGrab"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" " "

Section
  IfFileExists "$EXEDIR\python\pythonw.exe" run
    MessageBox MB_ICONSTOP "StreamGrab is incomplete: keep StreamGrab.exe together with its python, app and bin folders."
    Quit
  run:
  Exec '"$EXEDIR\python\pythonw.exe" -X utf8 "$EXEDIR\app\launcher.pyw"'
SectionEnd
