@echo off
rem Build AutoOrtho.xpl for Windows with MSVC (no X-Plane SDK needed).
rem Usage: build_win.bat [path\to\vcvars64.bat]
setlocal EnableDelayedExpansion
set "HERE=%~dp0"
set "VCVARS=%~1"
if not "%VCVARS%"=="" goto have_vcvars
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
rem Delayed expansion: the ")" of "(x86)" would end the clause otherwise.
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -property installationPath`) do set "VSPATH=%%i"
set "VCVARS=%VSPATH%\VC\Auxiliary\Build\vcvars64.bat"
:have_vcvars
call "%VCVARS%" >nul || exit /b 1
if not exist "%HERE%bin\win_x64" mkdir "%HERE%bin\win_x64"
cl /nologo /O2 /W3 /LD /MT /D_CRT_SECURE_NO_WARNINGS "%HERE%ao_gate.c" ^
   /Fe"%HERE%bin\win_x64\AutoOrtho.xpl" /Fo"%TEMP%\ao_gate.obj" ^
   /link ws2_32.lib kernel32.lib user32.lib /NOIMPLIB /NOEXP || exit /b 1
del "%HERE%bin\win_x64\AutoOrtho.exp" 2>nul
del "%HERE%bin\win_x64\AutoOrtho.lib" 2>nul
python "%HERE%stamp.py" 2>nul
echo Built %HERE%bin\win_x64\AutoOrtho.xpl
