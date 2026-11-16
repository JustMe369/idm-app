# Windows .exe — IDM-Like Download Manager

## Run it

1. Copy **`IDMLikeDownloadManager.exe`** to any folder (no install needed).
2. Double-click it.
3. A console window opens and your browser opens **http://127.0.0.1:8090**
4. Use the web UI to add URLs, control downloads, grab sites.
5. Close the console window (or press `Ctrl+C`) to stop the server.

## What's inside

The .exe is fully self-contained — it bundles Python, FastAPI, uvicorn and the web UI.
**No Python installation and no internet connection are required** to run it.

## Where things are stored

| Item | Location |
|---|---|
| Downloads | `C:\Users\<you>\Downloads` (subfolders per category) |
| Queue / history DB | `%LOCALAPPDATA%\IDM-Like\downloads.db` |
| Settings | stored in the same database |

## Windows SmartScreen

An unsigned .exe triggers a SmartScreen warning ("Windows protected your PC").
Click **More info → Run anyway**. This is normal for self-built executables.

## Firewall

The first run may prompt for network access. The app binds to `127.0.0.1:8090`
(local only). Allow it if you want other devices on your LAN to reach the UI —
otherwise the prompt can be dismissed.

## Rebuilding the .exe

From Linux (with Wine installed):

```bash
cd idm-app
bash build_windows.sh
# -> dist/IDMLikeDownloadManager.exe
```
