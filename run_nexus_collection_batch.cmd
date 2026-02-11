@echo off
setlocal

cd /d "%~dp0"

echo ================================================================
echo NexusCollectionBatch user runner
echo Working directory: %CD%
echo ================================================================
echo.
echo This will:
echo 1) Update Brave download prefs for unattended saves
echo 2) Start the guided runner
echo.

python .\scripts\set_brave_download_prefs.py
if errorlevel 1 (
  echo.
  echo [ERROR] Could not update Brave preferences.
  echo Run this file again after closing Brave.
  goto :end
)

echo.
python .\nexus_collection_batch.py

:end
echo.
echo Done.
pause
