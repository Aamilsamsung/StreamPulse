@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag and drop a video onto this file, or run:
  echo   clip-now.bat "C:\Videos\myvideo.mp4"
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Python 3 is required. Install Python and try again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg is missing. Install FFmpeg and add it to your PATH.
  echo Try: winget install Gyan.FFmpeg
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m backend.cli "%~1" --output "%~dp0output_clips" --preset dynamic --clips 3
if errorlevel 1 (
  echo Clipping failed. Review the error above.
) else (
  echo Clips saved in: %~dp0output_clips
  explorer "%~dp0output_clips"
)
pause
