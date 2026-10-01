@echo off
REM ============================================================
REM  Build script for Global Autocorrect
REM  Requires: pyinstaller installed in your Python environment
REM  Output:   dist\GlobalAutocorrect.exe
REM ============================================================

setlocal

REM --- Configuration ---
set SCRIPT_DIR=%~dp0
set SCRIPT_NAME=%SCRIPT_DIR%global_autocorrect.py
set EXE_NAME=GlobalAutocorrect

echo.
echo ============================================================
echo  Building %EXE_NAME%.exe
echo  Script: %SCRIPT_NAME%
echo ============================================================
echo.

REM --- Verify the script exists ---
if not exist "%SCRIPT_NAME%" (
    echo [ERROR] Script not found: %SCRIPT_NAME%
    echo.
    echo Files in %SCRIPT_DIR%:
    dir /b "%SCRIPT_DIR%"
    echo.
    echo Make sure your script is named exactly "global_autocorrect.py"
    echo and lives in the same folder as this batch file.
    pause
    exit /b 1
)

REM --- Verify PyInstaller is available ---
python -c "import PyInstaller" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller is not installed.
    echo Run: pip install pyinstaller
    pause
    exit /b 1
)

REM --- Run PyInstaller from the script's directory ---
pushd "%SCRIPT_DIR%"

echo Running PyInstaller...
echo.

python -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --clean ^
  --name %EXE_NAME% ^
  --hidden-import "pynput.keyboard._win32" ^
  --hidden-import "pynput.mouse._win32" ^
  --hidden-import "pystray._win32" ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "torch._C" ^
  --hidden-import "torch.backends.cudnn" ^
  --hidden-import "torch.backends.mkl" ^
  --hidden-import "language_tool_python" ^
  --collect-data "symspellpy" ^
  --collect-data "language_tool_python" ^
  --copy-metadata "torch" ^
  --copy-metadata "transformers" ^
  --copy-metadata "tqdm" ^
  --copy-metadata "regex" ^
  --copy-metadata "requests" ^
  --copy-metadata "packaging" ^
  --copy-metadata "filelock" ^
  --copy-metadata "numpy" ^
  --copy-metadata "huggingface-hub" ^
  --collect-all "transformers" ^
  --collect-all "pystray" ^
  --collect-all "pywin32" ^
  "%SCRIPT_NAME%"

set BUILD_RESULT=%errorlevel%
popd

if %BUILD_RESULT% neq 0 (
    echo.
    echo [ERROR] Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete: %SCRIPT_DIR%dist\%EXE_NAME%.exe
echo ============================================================
echo.
echo NOTE: The first run of the EXE will download DistilGPT-2
echo       (~330 MB) from Hugging Face. This is a one-time step.
echo.
pause