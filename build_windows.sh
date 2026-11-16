#!/usr/bin/env bash
# Build the Windows .exe from Linux using Wine + PyInstaller.
set -euo pipefail
export WINEPREFIX="${WINEPREFIX:-/root/.wine}"
export WINEDEBUG=-all
unset PYTHONPATH
unset PYTHONHOME
PY="/root/.wine/drive_c/users/root/AppData/Local/Programs/Python/Python312/python.exe"
cd "$(dirname "$0")"
echo ">> installing Windows dependencies..."
xvfb-run -a wine "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
xvfb-run -a wine "$PY" -m pip install fastapi "uvicorn[standard]" requests pyinstaller
echo ">> building IDMLikeDownloadManager.exe..."
xvfb-run -a wine "$PY" -m PyInstaller --noconfirm --clean idm.spec
echo
echo "Done: $(pwd)/dist/IDMLikeDownloadManager.exe"
ls -lh dist/IDMLikeDownloadManager.exe
