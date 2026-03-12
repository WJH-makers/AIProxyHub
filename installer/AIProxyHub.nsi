; AIProxyHub NSIS 安装脚本（生成 setup.exe）
;
; 目标：
; - 像 QQ/微信 一样：安装到本机 + 桌面图标 + 开始菜单 + 卸载入口
; - 程序本体只有一个 AIProxyHub.exe（内部已打包 CLIProxyAPI 与注册逻辑）
; - 用户数据默认保存到 %LOCALAPPDATA%\AIProxyHub（由 AIProxyHub 自身决定）
;
; 构建方式（推荐通过 scripts/build-installer-nsis.ps1 调用）：
;   makensis.exe /DAPP_VERSION=1.2.5 /DOUTFILE=E:\AIProxyHub\release\AIProxyHub-1.2.5-setup-win64.exe installer\AIProxyHub.nsi

Unicode True

!include "MUI2.nsh"

!ifndef APP_NAME
  !define APP_NAME "AIProxyHub"
!endif

!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif

!ifndef OUTFILE
  !define OUTFILE "${APP_NAME}-${APP_VERSION}-setup-win64.exe"
!endif

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTFILE}"

; 典型“安装版”安装目录（不需要管理员权限）
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
RequestExecutionLevel user

; ----------------------------
; Modern UI 基础页面
; ----------------------------

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"

; ----------------------------
; 安装内容
; ----------------------------

Section "AIProxyHub（必选）" SEC_CORE
  SectionIn RO

  SetOutPath "$INSTDIR"
  File "..\dist\AIProxyHub.exe"
  File "..\使用指南.md"
  File "..\API.md"

  ; 写入卸载器
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; 在“程序和功能”中登记（HKCU：当前用户）
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "WJH-makers（二次修改版）"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
SectionEnd

Section "创建桌面图标" SEC_DESKTOP
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\AIProxyHub.exe"
SectionEnd

Section "创建开始菜单" SEC_STARTMENU
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\AIProxyHub.exe"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

; ----------------------------
; 卸载
; ----------------------------

Section "Uninstall"
  ; 删除快捷方式
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"

  ; 删除程序文件（默认不删除用户数据目录：%LOCALAPPDATA%\AIProxyHub）
  Delete "$INSTDIR\AIProxyHub.exe"
  Delete "$INSTDIR\使用指南.md"
  Delete "$INSTDIR\API.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir  "$INSTDIR"

  ; 清理卸载登记
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd


