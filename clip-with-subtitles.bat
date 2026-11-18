@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
 echo Drag a video file onto this script to generate captioned clips.
 pause
 exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
 echo FFmpeg not found. Install with: winget install Gyan.FFmpeg
 pause
 exit /b 1
)
where ffprobe >nul 2>nul
if errorlevel 1 (
 echo FFprobe not found. Reinstall FFmpeg with ffprobe included.
 pause
 exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
 py -3 -m venv .venv
 if errorlevel 1 (
  echo Python 3 is required.
  pause
  exit /b 1
 )
)
".venv\Scripts\python.exe" -m pip install -r requirements-ai.txt
if errorlevel 1 (
 echo Dependency installation failed.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m backend.cli "%~1" --output "%~dp0output_clips" --preset clean --clips 3 --transcribe --subtitles
if errorlevel 1 (
 echo Captioned clipping failed. Check FFmpeg libass support and the error above.
) else (
 echo Clips are in output_clips
 explorer "%~dp0output_clips"
)
pause
