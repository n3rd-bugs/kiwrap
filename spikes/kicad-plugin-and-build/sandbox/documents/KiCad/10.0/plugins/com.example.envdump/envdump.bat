@echo off
echo KICAD_API_SOCKET=%KICAD_API_SOCKET%> "%~dp0envdump.txt"
echo KICAD_API_TOKEN=%KICAD_API_TOKEN%>> "%~dp0envdump.txt"
echo CWD=%CD%>> "%~dp0envdump.txt"
