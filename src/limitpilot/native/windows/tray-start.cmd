@echo off
rem LimitPilot 트레이 시작 (Windows) — limitpilot.pyz 와 같은 폴더에서 실행
rem Windows 내장 csc.exe(.NET Framework 4.x)로 tray.cs를 tray.exe로 컴파일한 뒤 실행한다.
rem 로컬에서 컴파일된 exe는 인터넷 흔적(MOTW)이 없어 SmartScreen 경고가 뜨지 않는다.
rem csc가 없는 환경에서는 기존 PowerShell 트레이(tray.ps1)로 폴백한다(기능 동일).
rem 로그인 시 자동 시작: 이 파일의 바로가기를 shell:startup 폴더에 넣으세요.
setlocal
set "DIR=%~dp0"
set "SRC=%DIR%tray.cs"
set "EXE=%DIR%tray.exe"

rem ── 내장 .NET Framework 컴파일러 탐색 (64비트 우선) ─────────────
set "CSC="
if exist "%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not defined CSC goto fallback
if not exist "%SRC%" goto fallback

rem ── tray.exe가 없거나 tray.cs가 더 새로우면 재컴파일 ────────────
set "NEED=1"
if exist "%EXE%" for /f %%i in ('powershell -NoProfile -Command "if ((Get-Item \"%SRC%\").LastWriteTime -gt (Get-Item \"%EXE%\").LastWriteTime) {1} else {0}"') do set "NEED=%%i"
if not "%NEED%"=="1" goto run

"%CSC%" /nologo /target:winexe /optimize+ /codepage:65001 /win32icon:"%DIR%LimitPilot.ico" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll /out:"%EXE%" "%SRC%"
if errorlevel 1 goto buildfailed

:run
start "" "%EXE%"
exit /b 0

:buildfailed
rem 트레이가 이미 떠 있으면 exe가 잠겨 컴파일이 실패한다 — 기존 exe로 계속 실행
if exist "%EXE%" goto run
goto fallback

:fallback
start "" powershell -NoProfile -ExecutionPolicy Bypass -Sta -WindowStyle Hidden -File "%DIR%tray.ps1"
exit /b 0
