; StreamGrab Windows installer (per-user, no admin rights needed).
; Built by windows/build.sh (makensis -DOUT=... -DICON=... -DSTAGE=... -DVERSION=... -DSIZE_KB=...).
Unicode true
!include "MUI2.nsh"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\StreamGrab"

Name "StreamGrab"
OutFile "${OUT}"
InstallDir "$LOCALAPPDATA\Programs\StreamGrab"
InstallDirRegKey HKCU "Software\StreamGrab" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "StreamGrab ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "StreamGrab"
VIAddVersionKey "FileDescription" "StreamGrab Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" " "

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This will install StreamGrab ${VERSION}, a Twitch and YouTube VOD, live stream and chat downloader.$\r$\n$\r$\nIt includes yt-dlp and FFmpeg, so nothing else needs to be installed. No administrator rights are needed.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN "$INSTDIR\StreamGrab.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start StreamGrab"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  IfFileExists "$LOCALAPPDATA\StreamGrab\instance.json" 0 done
    MessageBox MB_OKCANCEL|MB_ICONINFORMATION "If StreamGrab is open, close its window first, then click OK." /SD IDOK IDOK done
    Abort
  done:
FunctionEnd

Section "StreamGrab" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; Replace the previous version's program files (settings and downloads are kept elsewhere).
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\bin"
  File /r "${STAGE}/*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateShortcut "$SMPROGRAMS\StreamGrab.lnk" "$INSTDIR\StreamGrab.exe" "" "$INSTDIR\StreamGrab.exe" 0
  CreateShortcut "$DESKTOP\StreamGrab.lnk" "$INSTDIR\StreamGrab.exe" "" "$INSTDIR\StreamGrab.exe" 0

  WriteRegStr HKCU "Software\StreamGrab" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "StreamGrab"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "StreamGrab"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\StreamGrab.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" ${SIZE_KB}
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\StreamGrab.lnk"
  Delete "$DESKTOP\StreamGrab.lnk"
  ; Only StreamGrab's own files: never a recursive delete of the install folder itself.
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\bin"
  RMDir /r "$INSTDIR\licenses"
  Delete "$INSTDIR\StreamGrab.exe"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; The app window's browser profile and caches (downloads and settings are kept).
  RMDir /r "$LOCALAPPDATA\StreamGrab\window"
  RMDir /r "$LOCALAPPDATA\StreamGrab\emotes"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\StreamGrab"
SectionEnd
