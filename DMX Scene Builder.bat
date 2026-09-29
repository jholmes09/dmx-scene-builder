@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ========================================================
echo   DMX SCENE BUILDER, Jeff Holmes Presents
echo ========================================================
echo.

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Couldn't find Python 3 on this PC.
  echo Install it from https://www.python.org/downloads/
  echo On the first install screen, check "Add python.exe to PATH".
  echo Then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo Starting the server with: !PY!
echo Leave this window open while you work. Close it to stop DMX Scene Builder.
echo.

!PY! -m scenebuilder %*
set "STATUS=%errorlevel%"
if not "!STATUS!"=="0" (
  echo.
  echo DMX Scene Builder stopped with a problem, shown above.
  pause
)
exit /b %STATUS%
