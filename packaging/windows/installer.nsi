; =====================================================================================
; AI PCB Router — Windows setup wizard (NSIS 3, Modern UI 2)
;
; Build with packaging/windows/build_installer.py, which passes these defines:
;   APP_VERSION          user-facing version, e.g. 0.2.0-stage2
;   APP_VERSION_NUMERIC  four-part numeric version, e.g. 0.2.0.0
;   APP_PUBLISHER        from pyproject.toml
;   FILES_INSTALL        generated include: File commands for every bundled file
;   FILES_UNINSTALL      generated include: Delete/RMDir for exactly those files
;   OUTFILE, ASSETS_DIR, LICENSE_FILE
;
; Design decisions
; * Per-user install into %LOCALAPPDATA%\Programs (no administrator rights, no UAC
;   prompt). Settings, logs and API keys are per-user anyway.
; * The app is 64-bit only (bundled Python/Qt), Windows 10 1809+ (Qt 6 minimum). Setup
;   itself is a 32-bit program, like most NSIS installers: the official Windows build of
;   NSIS ships only x86 stubs. It therefore checks for 64-bit Windows and uses the 64-bit
;   registry view, so it writes exactly where the 64-bit app reads.
; * The uninstaller deletes the exact list of installed files and removes folders only
;   when empty. It never runs a recursive delete on the install folder.
; * The "Open with" option adds the app to Explorer's Open with list for .kicad_pcb.
;   It never takes over the default: KiCad keeps opening boards on double-click.
; * User data (settings, logs, workspaces, saved API keys) is kept on uninstall unless
;   the user ticks the option to remove it.
; =====================================================================================

!ifndef APP_VERSION | APP_VERSION_NUMERIC | APP_PUBLISHER | FILES_INSTALL | FILES_UNINSTALL | OUTFILE | ASSETS_DIR | LICENSE_FILE
  !error "Missing defines. Build with: python packaging/windows/build_installer.py"
!endif

Unicode true
Target x86-unicode
ManifestDPIAware true
SetCompressor /SOLID lzma
RequestExecutionLevel user

!define APP_NAME "AI PCB Router"
!define GUI_EXE "AI PCB Router.exe"
!define CLI_EXE "pcbrouter.exe"
!define APP_REGKEY "Software\AI PCB Router"
!define UNINSTALL_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\AI PCB Router"
!define PROGID "AIPCBRouter.kicad_pcb"
!define BOARD_EXT ".kicad_pcb"
!define MIN_WINDOWS_BUILD 17763 ; Windows 10 1809
!define SETUP_MUTEX "AIPCBRouter.Setup"

Name "${APP_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REGKEY}" "InstallDir"
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION_NUMERIC}"
VIFileVersion "${APP_VERSION_NUMERIC}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "MIT License"

!include MUI2.nsh
!include LogicLib.nsh
!include Sections.nsh
!include FileFunc.nsh
!include WinVer.nsh
!include x64.nsh

Var PreviousDir
Var PreviousVersion

; ------------------------------------------------------------------ interface

!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
!define MUI_ICON "${ASSETS_DIR}\app.ico"
!define MUI_UNICON "${ASSETS_DIR}\app.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${ASSETS_DIR}\wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${ASSETS_DIR}\wizard.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${ASSETS_DIR}\header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "${ASSETS_DIR}\header.bmp"
!define MUI_COMPONENTSPAGE_SMALLDESC

; ------------------------------------------------------------------ installer pages

!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APP_NAME} ${APP_VERSION} Setup"
!define MUI_WELCOMEPAGE_TEXT "This wizard installs ${APP_NAME}, a read-only KiCad board inspector with an AI planning assistant.$\r$\n$\r$\n\
${APP_NAME} never modifies your KiCad files. This version does not route or edit copper.$\r$\n$\r$\n\
It is installed for your Windows account only, so no administrator rights are needed.$\r$\n$\r$\n\
$_CLICK"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LICENSE_FILE}"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "${APP_NAME} is installed"
; Keep this short: the page has room for about five lines above the checkbox.
!define MUI_FINISHPAGE_TEXT "Setup has finished installing ${APP_NAME} ${APP_VERSION}.$\r$\n$\r$\n\
To use the AI assistant, open AI > Configure AI Providers. API keys are kept in Windows Credential Manager."
!define MUI_FINISHPAGE_RUN "$INSTDIR\${GUI_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP_NAME}"
!insertmacro MUI_PAGE_FINISH

; ------------------------------------------------------------------ uninstaller pages

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------ helpers

; Probe one executable: a running .exe cannot be opened for writing (Windows maps it
; with FILE_SHARE_READ only). Nothing is written. Sets $1 to the path if it is in use.
; (The error flag is tested once, right after FileOpen: testing it also clears it.)
!macro _PROBE_EXE_IN_USE FILE
  ${If} ${FileExists} "${FILE}"
    ClearErrors
    FileOpen $0 "${FILE}" a
    ${If} ${Errors}
      StrCpy $1 "${FILE}"
    ${Else}
      FileClose $0
    ${EndIf}
  ${EndIf}
!macroend

; Retry/Cancel prompt while the app installed in DIR is running; Cancel quits Setup
; (exit code 5). Silent mode behaves like Cancel.
!macro ENSURE_APP_CLOSED DIR
  _eac_retry:
  StrCpy $1 ""
  !insertmacro _PROBE_EXE_IN_USE "${DIR}\${GUI_EXE}"
  !insertmacro _PROBE_EXE_IN_USE "${DIR}\${CLI_EXE}"
  ${If} $1 != ""
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
      "${APP_NAME} is running.$\r$\n$\r$\nClose all ${APP_NAME} windows, then click Retry." \
      /SD IDCANCEL IDRETRY _eac_retry
    SetErrorLevel 5
    Quit
  ${EndIf}
!macroend

; Explorer caches file-type information; tell it that associations changed.
!macro REFRESH_SHELL_ASSOCIATIONS
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend

; ------------------------------------------------------------------ installer sections

Section "!${APP_NAME} (required)" SEC_CORE
  SectionIn RO
  SetShellVarContext current

  ; Upgrade: the previous version's own uninstaller removes exactly the files it
  ; installed (its file list may differ from this version's). User data is kept.
  ${If} $PreviousDir != ""
  ${AndIf} ${FileExists} "$PreviousDir\Uninstall.exe"
    DetailPrint "Removing previous version $PreviousVersion from $PreviousDir"
    ExecWait '"$PreviousDir\Uninstall.exe" /S _?=$PreviousDir' $0
    DetailPrint "Previous version removed (exit code $0)"
    Delete "$PreviousDir\Uninstall.exe"
    RMDir "$PreviousDir"
  ${EndIf}

  !include "${FILES_INSTALL}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "${APP_REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${APP_REGKEY}" "Version" "${APP_VERSION}"

  ; Settings > Apps > Installed apps
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "DisplayIcon" "$INSTDIR\${GUI_EXE},0"
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_REGKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_REGKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTALL_REGKEY}" "EstimatedSize" "$0"
SectionEnd

Section "Start menu shortcut" SEC_STARTMENU
  SetShellVarContext current
  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${GUI_EXE}" "" "$INSTDIR\${GUI_EXE}" 0
SectionEnd

Section "Desktop shortcut" SEC_DESKTOP
  SetShellVarContext current
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${GUI_EXE}" "" "$INSTDIR\${GUI_EXE}" 0
SectionEnd

Section "'Open with' entry for KiCad boards" SEC_OPENWITH
  ; Per-user ProgID + OpenWithProgids value: adds a choice, changes no default.
  WriteRegStr HKCU "Software\Classes\${PROGID}" "" "KiCad PCB (${APP_NAME})"
  WriteRegStr HKCU "Software\Classes\${PROGID}" "FriendlyTypeName" "KiCad PCB"
  WriteRegStr HKCU "Software\Classes\${PROGID}\DefaultIcon" "" "$INSTDIR\${GUI_EXE},0"
  WriteRegStr HKCU "Software\Classes\${PROGID}\shell\open\command" "" '"$INSTDIR\${GUI_EXE}" "%1"'
  WriteRegNone HKCU "Software\Classes\${BOARD_EXT}\OpenWithProgids" "${PROGID}"
  WriteRegStr HKCU "Software\Classes\Applications\${GUI_EXE}" "FriendlyAppName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Classes\Applications\${GUI_EXE}\SupportedTypes" "${BOARD_EXT}" ""
  WriteRegStr HKCU "Software\Classes\Applications\${GUI_EXE}\shell\open\command" "" '"$INSTDIR\${GUI_EXE}" "%1"'
  !insertmacro REFRESH_SHELL_ASSOCIATIONS
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_CORE} "The application, the pcbrouter command-line tool and the uninstaller."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_STARTMENU} "Adds ${APP_NAME} to the Start menu."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} "Adds a shortcut to your desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_OPENWITH} "Lists ${APP_NAME} under 'Open with' for .kicad_pcb files. KiCad stays the default program."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
  SetShellVarContext current

  ${IfNot} ${RunningX64}
  ${OrIfNot} ${AtLeastBuild} ${MIN_WINDOWS_BUILD}
    MessageBox MB_OK|MB_ICONSTOP "${APP_NAME} requires 64-bit Windows 10 version 1809 or later." /SD IDOK
    SetErrorLevel 3
    Quit
  ${EndIf}
  SetRegView 64

  ; Only one copy of Setup at a time.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "${SETUP_MUTEX}") p .r1 ?e'
  Pop $0
  ${If} $0 == 183 ; ERROR_ALREADY_EXISTS
    MessageBox MB_OK|MB_ICONEXCLAMATION "${APP_NAME} Setup is already running." /SD IDOK
    SetErrorLevel 4
    Quit
  ${EndIf}

  ReadRegStr $PreviousDir HKCU "${UNINSTALL_REGKEY}" "InstallLocation"
  ReadRegStr $PreviousVersion HKCU "${UNINSTALL_REGKEY}" "DisplayVersion"
  ${If} $PreviousDir != ""
  ${AndIf} ${FileExists} "$PreviousDir\Uninstall.exe"
    !insertmacro ENSURE_APP_CLOSED "$PreviousDir"
    ${If} ${Cmd} `MessageBox MB_OKCANCEL|MB_ICONINFORMATION "${APP_NAME} $PreviousVersion is already installed.$\r$\n$\r$\nSetup will replace it with version ${APP_VERSION}. Your settings and saved API keys are kept." /SD IDOK IDCANCEL`
      Quit
    ${EndIf}
  ${Else}
    StrCpy $PreviousDir ""
  ${EndIf}
FunctionEnd

; ------------------------------------------------------------------ uninstaller sections

Section "!un.${APP_NAME} program files (required)" SEC_UN_CORE
  SectionIn RO
  SetShellVarContext current

  ; Must run before the program files are deleted: it uses pcbrouter.exe.
  Call un.ForgetApiKeysIfRequested

  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  ; Remove only what Setup added. Never delete the .kicad_pcb key or the
  ; OpenWithProgids key: other programs (KiCad) keep their values there.
  DeleteRegValue HKCU "Software\Classes\${BOARD_EXT}\OpenWithProgids" "${PROGID}"
  DeleteRegKey HKCU "Software\Classes\${PROGID}"
  DeleteRegKey HKCU "Software\Classes\Applications\${GUI_EXE}"
  !insertmacro REFRESH_SHELL_ASSOCIATIONS

  !include "${FILES_UNINSTALL}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ${If} ${FileExists} "$INSTDIR\*.*"
    DetailPrint "Kept $INSTDIR: it contains files that Setup did not install."
  ${EndIf}

  DeleteRegKey HKCU "${UNINSTALL_REGKEY}"
  DeleteRegKey HKCU "${APP_REGKEY}"
SectionEnd

Section /o "un.My settings, logs, workspaces and saved API keys" SEC_UN_USERDATA
  SetShellVarContext current
  ; Fixed application folders (see pcbrouter.utils.paths), never user-chosen paths.
  DetailPrint "Removing $APPDATA\${APP_NAME}"
  RMDir /r "$APPDATA\${APP_NAME}"
  DetailPrint "Removing $LOCALAPPDATA\${APP_NAME}"
  RMDir /r "$LOCALAPPDATA\${APP_NAME}"
SectionEnd

!insertmacro MUI_UNFUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_UN_CORE} "Removes the program, its shortcuts and its 'Open with' entry."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_UN_USERDATA} "Also deletes your settings, logs, AI session history, and the API keys saved in Windows Credential Manager. Leave unticked to keep them for a later reinstall."
!insertmacro MUI_UNFUNCTION_DESCRIPTION_END

Function un.ForgetApiKeysIfRequested
  ${If} ${SectionIsSelected} ${SEC_UN_USERDATA}
  ${AndIf} ${FileExists} "$INSTDIR\${CLI_EXE}"
    DetailPrint "Removing saved API keys from Windows Credential Manager"
    nsExec::ExecToLog '"$INSTDIR\${CLI_EXE}" --forget-api-keys'
    Pop $0
    ${If} $0 != 0
      DetailPrint "Could not remove saved API keys (result: $0). Delete the entries containing 'ai-pcb-router' in Credential Manager."
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.onInit
  SetShellVarContext current
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
  ; /REMOVEUSERDATA selects the user-data option (for silent uninstalls: /S /REMOVEUSERDATA).
  ${un.GetParameters} $R0
  ClearErrors
  ${un.GetOptions} $R0 "/REMOVEUSERDATA" $R1
  ${IfNot} ${Errors}
    !insertmacro SelectSection ${SEC_UN_USERDATA}
  ${EndIf}
  !insertmacro ENSURE_APP_CLOSED "$INSTDIR"
FunctionEnd
