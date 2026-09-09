@echo off
REM Launches the PowerShell wrapper with the execution policy bypassed for this
REM process only, so the unsigned .ps1 runs without changing machine settings.
REM Passes any arguments straight through, e.g.:
REM   Run-PhotoRescue.cmd -Source 1 -Out E:\Recovered -Phases sweep,bin,vss,carve
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-PhotoRescue.ps1" %*
if errorlevel 1 pause
