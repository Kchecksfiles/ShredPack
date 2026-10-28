@echo off
rem Builds ShredPackMenu.dll (x64) and runs its self-test. Output: shell-ext\out\
rem Run it from an "x64 Native Tools Command Prompt for VS", or set VCVARS to the full path of
rem vcvars64.bat first (the Windows CI does that).
setlocal
if defined VCVARS call "%VCVARS%" >nul || exit /b 1
cd /d "%~dp0"
if not exist out mkdir out

rem /MT: the C runtime is linked in, so the DLL needs no Visual C++ redistributable on the user's PC.
cl /nologo /LD /MT /O2 /EHsc /std:c++17 /W3 /DUNICODE /D_UNICODE ShredPackMenu.cpp /Foout\ /Feout\ShredPackMenu.dll /link /DEF:ShredPackMenu.def ole32.lib shell32.lib shlwapi.lib user32.lib || exit /b 1
cl /nologo /MT /O2 /EHsc /std:c++17 /W3 /DUNICODE /D_UNICODE ShredPackMenuTest.cpp /Foout\ /Feout\ShredPackMenuTest.exe /link ole32.lib shell32.lib shlwapi.lib user32.lib || exit /b 1

rem The self-test loads the DLL like COM does and checks titles + that Invoke starts ShredPack.exe.
out\ShredPackMenuTest.exe || exit /b 1
echo Built and tested out\ShredPackMenu.dll
