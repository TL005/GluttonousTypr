@echo off
REM ============================================================
REM  EnviousTypr v9.7 — Windows build script
REM  Input :  EnviousTypr9.7.py
REM  Output:  dist\EnviousTypr.exe
REM
REM  Requirements:
REM    - Python 3.10+
REM    - pip install pyinstaller
REM    - Runtime dependencies installed (see requirements below)
REM ============================================================

setlocal EnableDelayedExpansion

REM ---------- Configuration ----------
set APP_NAME=EnviousTypr
set SCRIPT_NAME=EnviousTypr9.7.py
set ICON_FILE=envioustypr.ico
set SCRIPT_DIR=%~dp0
set SCRIPT_PATH=%SCRIPT_DIR%%SCRIPT_NAME%

REM ---------- Header ----------
echo.
echo ============================================================
echo   Building %APP_NAME%.exe
echo ============================================================
echo   Script dir : %SCRIPT_DIR%
echo   Script     : %SCRIPT_NAME%
echo.

REM ---------- Sanity: script exists ----------
if not exist "%SCRIPT_PATH%" (
    echo [ERROR] Script not found: "%SCRIPT_PATH%"
    echo.
    echo Files in %SCRIPT_DIR%:
    dir /b "%SCRIPT_DIR%"
    echo.
    echo Make sure "%SCRIPT_NAME%" is in the same folder as build.bat.
    pause
    exit /b 1
)

REM ---------- Sanity: PyInstaller ----------
python -c "import PyInstaller" >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] PyInstaller is not installed.
    echo Run: pip install pyinstaller
    pause
    exit /b 1
)

REM ---------- Sanity: core runtime deps ----------
python -c "import pynput, symspellpy, transformers, torch" >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Core runtime dependencies missing.
    echo Run these two commands, then retry:
    echo   pip install pynput symspellpy transformers pystray pillow pygetwindow pywin32 onnxruntime "optimum[onnxruntime]" peft datasets language-tool-python
    echo   pip install torch --index-url https://download.pytorch.org/whl/cpu
    pause
    exit /b 1
)

REM ---------- Clean previous artifacts ----------
echo [1/4] Cleaning previous build artifacts...
if exist "%SCRIPT_DIR%build" rmdir /s /q "%SCRIPT_DIR%build"
if exist "%SCRIPT_DIR%dist"  rmdir /s /q "%SCRIPT_DIR%dist"
if exist "%SCRIPT_DIR%%APP_NAME%.spec" del /q "%SCRIPT_DIR%%APP_NAME%.spec"
echo      Done.
echo.

REM ---------- Icon flag ----------
set ICON_FLAG=
if exist "%SCRIPT_DIR%%ICON_FILE%" (
    set ICON_FLAG=--icon "%SCRIPT_DIR%%ICON_FILE%"
    echo [2/4] Using icon: %ICON_FILE%
) else (
    echo [2/4] No icon file found; building without custom icon.
    echo      Optional: place a 256x256 %ICON_FILE% next to this script.
)
echo.

REM ---------- Run PyInstaller ----------
echo [3/4] Running PyInstaller (may take 3-10 minutes)...
echo.

pushd "%SCRIPT_DIR%"

python -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --clean ^
  --name "%APP_NAME%" ^
  !ICON_FLAG! ^
  --hidden-import "pynput.keyboard._win32" ^
  --hidden-import "pynput.mouse._win32" ^
  --hidden-import "pystray._win32" ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "torch._C" ^
  --hidden-import "torch.backends.cudnn" ^
  --hidden-import "torch.backends.mkl" ^
  --hidden-import "language_tool_python" ^
  --hidden-import "win32com" ^
  --hidden-import "win32com.client" ^
  --hidden-import "pythoncom" ^
  --hidden-import "pywintypes" ^
  --collect-data "symspellpy" ^
  --collect-data "language_tool_python" ^
  --collect-all "pystray" ^
  --collect-all "pywin32" ^
  --collect-submodules "win32com" ^
  --copy-metadata "torch" ^
  --copy-metadata "transformers" ^
  --copy-metadata "tqdm" ^
  --copy-metadata "regex" ^
  --copy-metadata "requests" ^
  --copy-metadata "packaging" ^
  --copy-metadata "filelock" ^
  --copy-metadata "numpy" ^
  --copy-metadata "huggingface-hub" ^
  --copy-metadata "safetensors" ^
  --copy-metadata "tokenizers" ^
  --copy-metadata "pyyaml" ^
  --copy-metadata "symspellpy" ^
  --copy-metadata "pynput" ^
  "%SCRIPT_NAME%"

set BUILD_RESULT=!errorlevel!
popd

if !BUILD_RESULT! neq 0 (
    echo.
    echo ============================================================
    echo   [FAILED] PyInstaller exited with error !BUILD_RESULT!
    echo ============================================================
    echo.
    echo Common fixes:
    echo   - Upgrade PyInstaller:  pip install --upgrade pyinstaller
    echo   - Reinstall deps:       pip install -r requirements.txt
    echo   - Delete build\ and dist\, then retry
    echo.
    pause
    exit /b 1
)

REM ---------- Verify output ----------
if not exist "%SCRIPT_DIR%dist\%APP_NAME%.exe" (
    echo [ERROR] Build reported success but EXE not found at:
    echo         %SCRIPT_DIR%dist\%APP_NAME%.exe
    pause
    exit /b 1
)

for %%F in ("%SCRIPT_DIR%dist\%APP_NAME%.exe") do (
    set EXE_SIZE=%%~zF
)

echo.
echo ============================================================
echo   BUILD COMPLETE
echo ============================================================
echo.
echo   Output: %SCRIPT_DIR%dist\%APP_NAME%.exe
echo   Size  : !EXE_SIZE! bytes
echo.
echo   First run will download DistilGPT-2 (~330 MB) from Hugging Face.
echo   Subsequent runs load from cache in ~3-5 seconds.
echo.
echo   To autostart:
echo     - Launch the EXE, right-click the tray icon,
echo       and enable "Start at logon"
echo.
pause
exit /b 0
