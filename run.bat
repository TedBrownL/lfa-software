@echo off
REM Launch the LFA temperature acquisition GUI.
REM Uses the casimir conda env, which lives on drive D.
cd /d "%~dp0"
"D:\MINICONDA\envs\casimir\python.exe" -m lfa
if errorlevel 1 pause
