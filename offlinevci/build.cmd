@echo off
rem Rebuilds laertes_vci32.dll, the offline VCI embedded in laertes.exe (needs zig: https://ziglang.org).
rem The DLL is committed, so building laertes.exe does not need this.
cd /d "%~dp0"
zig cc -target x86-windows-gnu -shared -O2 -s -o laertes_vci32.dll offlinevci.c offlinevci.def -lws2_32 || exit /b 1
del /q laertes_vci32.lib offlinevci.lib 2>nul
echo Done: %CD%\laertes_vci32.dll
