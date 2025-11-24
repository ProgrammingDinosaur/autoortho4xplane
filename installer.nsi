!define PRODUCT_NAME "AutoOrtho"
;!define PRODUCT_VERSION "1.4.2"
;!define PY_VERSION "3.12.9"
;!define PY_MAJOR_VERSION "3.12"
;!define BITNESS "32"
!define ARCH_TAG ""
;!define INSTALLER_NAME "AutoOrtho_1.4.2.exe"
!define INSTALLER_NAME "AutoOrtho.exe"
!define PRODUCT_ICON "ao-icon.ico"

; Marker file to tell the uninstaller that it's a user installation
!define USER_INSTALL_MARKER _user_install_marker

; Use best compression to reduce file entropy (looks less suspicious)
SetCompressor /SOLID lzma
SetCompressorDictSize 32

!if "${NSIS_PACKEDVERSION}" >= 0x03000000
  Unicode true
  ManifestDPIAware true
!endif

!define MULTIUSER_EXECUTIONLEVEL Highest
!define MULTIUSER_INSTALLMODE_DEFAULT_CURRENTUSER
!define MULTIUSER_MUI
!define MULTIUSER_INSTALLMODE_COMMANDLINE
!define MULTIUSER_INSTALLMODE_INSTDIR "AutoOrtho"
!include MultiUser.nsh
!include FileFunc.nsh

; Modern UI installer stuff
!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON "ao-icon.ico"
!define MUI_UNICON "ao-icon.ico"

; UI pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MULTIUSER_PAGE_INSTALLMODE
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "${INSTALLER_NAME}"
ShowInstDetails show

; Add version info and manifest to reduce AV false positives
VIProductVersion "0.1.4.2"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "CompanyName" "AutoOrtho Project"
VIAddVersionKey "LegalCopyright" "© AutoOrtho Project"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} Installer"
VIAddVersionKey "FileVersion" "0.1.0"
VIAddVersionKey "ProductVersion" "1.4.2"

; Request admin rights explicitly in manifest
RequestExecutionLevel admin

; === CODE SIGNING (Uncomment when you have a certificate) ===
; !finalize 'signtool.exe sign /f "certificate.p12" /p "password" /t "http://timestamp.comodoca.com" "%1"'
; Or for EV certificates:
; !finalize 'signtool.exe sign /sha1 "THUMBPRINT" /t "http://timestamp.comodoca.com" "%1"'

Var cmdLineInstallDir

Section -SETTINGS
  SetOutPath "$INSTDIR"
  SetOverwrite ifnewer
SectionEnd


Section "!${PRODUCT_NAME}" sec_app
  SetRegView 32
  SectionIn RO
  
  ; === SAFETY VALIDATION CHECKS ===
  ; Check if installing to a dangerous location
  Call ValidateInstallLocation
  
  File ${PRODUCT_ICON}

    ; Copy pkgs data
    ; SetOutPath "$INSTDIR\pkgs"
    ; File /r "pkgs\*.*"

  SetOutPath "$INSTDIR"

  ; Marker file for per-user install
  StrCmp $MultiUser.InstallMode CurrentUser 0 +3
    FileOpen $0 "$INSTDIR\${USER_INSTALL_MARKER}" w
    FileClose $0
    SetFileAttributes "$INSTDIR\${USER_INSTALL_MARKER}" HIDDEN

      ; Install files
  ;  SetOutPath "$INSTDIR"
  ;    File "ao-icon.ico"
  ;    File "AutoOrtho.launch.pyw"

  ; Install directories
  ;  SetOutPath "$INSTDIR\Python"
  ;  File /r "Python\*.*"
  ;  SetOutPath "$INSTDIR\templates"
  ;  File /r "templates\*.*"
  ;  SetOutPath "$INSTDIR\windows"
  ;  File /r "windows\*.*"
  ;  SetOutPath "$INSTDIR\aoimage"
  ;  File /r "aoimage\*.*"
  ;  SetOutPath "$INSTDIR\imgs"
  ;  File /r "imgs\*.*"
   SetOutPath "$INSTDIR"
   File /r "__main__.dist\*.*"
  ; File /r "autoortho_release\*.*"


  ; Install shortcuts
  ; The output path becomes the working directory for shortcuts
  SetOutPath "%HOMEDRIVE%\%HOMEPATH%"
    CreateShortCut "$SMPROGRAMS\AutoOrtho.lnk" "$INSTDIR\autoortho_win.exe" "$INSTDIR\ao-icon.ico"
  ;  CreateShortCut "$SMPROGRAMS\AutoOrtho.lnk" "$INSTDIR\Python\pythonw.exe" \
  ;    '"$INSTDIR\AutoOrtho.launch.pyw"' "$INSTDIR\ao-icon.ico"
  SetOutPath "$INSTDIR"


  ; Byte-compile Python files.
  ;DetailPrint "Byte-compiling Python modules..."
  ;nsExec::ExecToLog '"$INSTDIR\Python\python" -m compileall -q "$INSTDIR\pkgs"'
  WriteUninstaller $INSTDIR\uninstall.exe
  ; Add ourselves to Add/remove programs
  WriteRegStr SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "InstallLocation" "$INSTDIR"
  WriteRegStr SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "DisplayIcon" "$INSTDIR\${PRODUCT_ICON}"
  WriteRegStr SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegDWORD SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "NoModify" 1
  WriteRegDWORD SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
                   "NoRepair" 1

  ; Check if we need to reboot
  IfRebootFlag 0 noreboot
    MessageBox MB_YESNO "A reboot is required to finish the installation. Do you wish to reboot now?" \
                /SD IDNO IDNO noreboot
      Reboot
  noreboot:
SectionEnd

Section "Uninstall"
  SetRegView 32
  SetShellVarContext all
  IfFileExists "$INSTDIR\${USER_INSTALL_MARKER}" 0 +3
    SetShellVarContext current
    Delete "$INSTDIR\${USER_INSTALL_MARKER}"

  ; === SAFETY VALIDATION FOR UNINSTALLER ===
  ; Verify this is actually an AutoOrtho installation before deleting everything
  IfFileExists "$INSTDIR\autoortho_win.exe" autoortho_confirmed
  IfFileExists "$INSTDIR\ao-icon.ico" autoortho_confirmed
  
  ; No AutoOrtho files found - this is dangerous!
  MessageBox MB_YESNO|MB_ICONEXCLAMATION "WARNING: AutoOrtho files not detected in '$INSTDIR'$\n$\nThis may not be a valid AutoOrtho installation directory.$\nDeleting this directory could remove important files!$\n$\nAre you absolutely sure you want to continue?" IDYES force_uninstall
  
  ; User chose not to continue - just clean registry and exit
  DeleteRegKey SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
  Delete "$SMPROGRAMS\AutoOrtho.lnk"
  MessageBox MB_OK "Uninstall cancelled for safety. Registry entries have been cleaned."
  Goto uninstall_end

autoortho_confirmed:
force_uninstall:
  ; Show what will be deleted
  MessageBox MB_YESNO|MB_ICONQUESTION "AutoOrtho will be completely removed from:$\n$INSTDIR$\n$\nThis will delete ALL files and folders in this directory.$\n$\nContinue?" IDYES proceed_uninstall
  
  ; User cancelled
  MessageBox MB_OK "Uninstall cancelled by user."
  Goto uninstall_end

proceed_uninstall:
  Delete $INSTDIR\uninstall.exe
  Delete "$INSTDIR\${PRODUCT_ICON}"
  ;RMDir /r "$INSTDIR\pkgs"

  ; Remove ourselves from %PATH%

  ; Uninstall files
  ;  Delete "$INSTDIR\ao-icon.ico"
  ;  Delete "$INSTDIR\AutoOrtho.launch.pyw"
  ; Uninstall directories
  ;  RMDir /r "$INSTDIR\Python"
  ;  RMDir /r "$INSTDIR\templates"
  ;  RMDir /r "$INSTDIR\windows"
  ;  RMDir /r "$INSTDIR\aoimage"
  ;  RMDir /r "$INSTDIR\imgs"

  ; Uninstall shortcuts
      Delete "$SMPROGRAMS\AutoOrtho.lnk"
  RMDir /r $INSTDIR
  DeleteRegKey SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

uninstall_end:
SectionEnd


; Functions

Function .onMouseOverSection
    ; Find which section the mouse is over, and set the corresponding description.
    FindWindow $R0 "#32770" "" $HWNDPARENT
    GetDlgItem $R0 $R0 1043 ; description item (must be added to the UI)

    StrCmp $0 ${sec_app} "" +2
      SendMessage $R0 ${WM_SETTEXT} 0 "STR:${PRODUCT_NAME}"

FunctionEnd

Function .onInit
  ; Multiuser.nsh breaks /D command line parameter. Parse /INSTDIR instead.
  ; Cribbing from https://nsis-dev.github.io/NSIS-Forums/html/t-299280.html
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} '$0' "/INSTDIR=" $1
  IfErrors +2  ; Error means flag not found
    StrCpy $cmdLineInstallDir $1
  ClearErrors
  
  ;Exec $INSTDIR\uninstall.exe 
  ;RMDir /r $INSTDIR

  ;  ${If} ${Silent}
  ;      ReadRegStr $R0 HKLM "${PROJECT_REG_UNINSTALL_KEY}" "QuietUninstallString"
  ;  ${Else}
  ;      ReadRegStr $R0 HKLM "${PROJECT_REG_UNINSTALL_KEY}" "UninstallString"
  ;  ${EndIf}
  ;  ExecWait "$R0"
  
  ReadRegStr $R0 SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString"
  ;ReadRegStr $R0 HKLM "${PROJECT_REG_UNINSTALL_KEY}" "UninstallString"
  ExecWait "$R0"

  !insertmacro MULTIUSER_INIT

  ; If cmd line included /INSTDIR, override the install dir set by MultiUser
  StrCmp $cmdLineInstallDir "" +2
    StrCpy $INSTDIR $cmdLineInstallDir
FunctionEnd

Function un.onInit
  !insertmacro MULTIUSER_UNINIT
FunctionEnd

; ===== SAFETY VALIDATION FUNCTION =====
Function ValidateInstallLocation
  Push $0
  Push $1
  Push $2
  
  ; Check if installing directly to X-Plane root
  IfFileExists "$INSTDIR\X-Plane.exe" dangerous_xplane_root
  IfFileExists "$INSTDIR\X-Plane 12.exe" dangerous_xplane_root
  IfFileExists "$INSTDIR\X-Plane 11.exe" dangerous_xplane_root
  
  ; Check if directory is not empty (except for previous AutoOrtho install)
  Call CheckDirectoryContents
  Pop $2
  
  ; Debug: Show what we detected (remove this after testing)
  ; MessageBox MB_OK "Debug: Directory check result = '$2'"
  
  StrCmp $2 "safe" safe_location
  StrCmp $2 "autoortho" safe_location  ; Previous AutoOrtho install
  
  ; Directory not empty and doesn't contain AutoOrtho
  MessageBox MB_YESNO|MB_ICONEXCLAMATION "Warning: The target directory '$INSTDIR' is not empty.$\n$\nInstalling AutoOrtho here may cause issues during uninstallation.$\n$\nRecommended: Choose an empty directory or dedicated AutoOrtho folder.$\n$\nContinue anyway?" IDYES safe_location
  Abort


dangerous_xplane_root:
  MessageBox MB_OK|MB_ICONSTOP "DANGER: You are trying to install to the X-Plane root directory!$\n$\nThis could cause your entire X-Plane installation to be deleted during uninstallation.$\n$\nPlease choose a different directory like:$\n• C:\AutoOrtho$\n• D:\AutoOrtho$\n$\nInstallation aborted for your safety."
  Abort

safe_location:
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function CheckDirectoryContents
  Push $0
  Push $1
  Push $2
  
  ; First check if directory is empty using proper NSIS method
  FindFirst $0 $1 "$INSTDIR\*.*"
  StrCmp $1 "." 0 _notempty
    FindNext $0 $1
    StrCmp $1 ".." 0 _notempty
      ClearErrors
      FindNext $0 $1
      IfErrors 0 _notempty
        ; Directory is empty
        FindClose $0
        StrCpy $0 "safe"
        Goto done
        
_notempty:
  ; Directory not empty - check what's in it
  FindClose $0
  ClearErrors
  
  ; Start over and check each file
  StrCpy $2 "autoortho"  ; Assume it's all AutoOrtho files until proven otherwise
  FindFirst $0 $1 "$INSTDIR\*.*"
  
check_loop:
  StrCmp $1 "." next_file
  StrCmp $1 ".." next_file
  
  ; Check if it's an AutoOrtho file
  StrCmp $1 "autoortho_win.exe" autoortho_file
  StrCmp $1 "ao-icon.ico" autoortho_file
  StrCmp $1 "_user_install_marker" autoortho_file
  StrCmp $1 "uninstall.exe" autoortho_file
  
  ; Found non-AutoOrtho file
  StrCpy $2 "not_empty"
  Goto end_check

autoortho_file:
next_file:
  FindNext $0 $1
  StrCmp $0 "" end_check  ; End of files
  Goto check_loop

end_check:
  FindClose $0
  StrCpy $0 $2  ; Put result in $0

done:
  ; Restore registers and return result
  Pop $2    ; Restore original $2  
  Pop $1    ; Restore original $1
  Exch $0   ; Exchange result with saved $0 (result now on top of stack)
FunctionEnd

