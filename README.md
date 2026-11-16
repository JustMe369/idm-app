# IDM-Like Download Manager

A from-scratch, open-source download accelerator in the spirit of **Internet Download Manager (IDM)** — built with Python + a web UI.

## Features

| IDM feature | Here |
|---|---|
| **Dynamic file segmentation** | Splits the largest remaining segment in half whenever a connection is free (IDM's in-half rule) |
| **Multi-connection acceleration** | Up to 32 parallel range requests per file |
| **Resume** | Progress persisted to SQLite every few seconds; resume after pause, crash, or restart |
| **Download queue** | Add now / queue-only; configurable simultaneous downloads |
| **Speed limiter** | Per-download and global cap (shared token bucket across all segments) |
| **Categories** | Auto-folders: Videos, Music, Programs, Documents, Archives, Images |
| **Browser integration** | Bookmarklet + `/api/capture` endpoint + Site grabber |
| **Site grabber** | Extract every link from a page (with regex filters) and add all to the queue |
| **Live UI** | Dark theme, per-segment bars, speed, ETA, status, filters |
| **Single-connection fallback** | Servers without `Accept-Ranges` download sequentially (no resume) |
| **Error recovery** | Automatic retry on connection errors |

## Run

```bash
cd ~/idm-app
pip install -r requirements.txt
python3 app.py            # serves on http://0.0.0.0:8090
```

Open **http://<host>:8090** in a browser.

To run it as a service (survives reboot):

```bash
cp idm.service /etc/systemd/system/idm.service
systemctl enable --now idm
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/downloads` | List downloads with progress |
| POST | `/api/downloads` | Add `{url, filename?, category?, max_conn?, speed_limit?, start?}` |
| POST | `/api/downloads/{id}/pause` · `/resume` · `/cancel` | Control |
| DELETE | `/api/downloads/{id}` | Remove |
| POST | `/api/downloads/{id}/speed` | Change speed limit `{speed_limit: bytes/sec}` |
| GET/POST | `/api/settings` | Download dir, max concurrent, default connections, global limit |
| POST | `/api/capture` | Add a URL from the browser bookmarklet |
| POST | `/api/grab` | `{url, pattern?}` → list matching links |
| POST | `/api/grab/add` | Add all matching links to the queue |
| GET | `/api/file/{id}` | Serve a completed file |

## Browser integration

Drag this bookmarklet to your bookmarks bar; clicking it sends the current page to the manager:

```
javascript:(function(){var u=encodeURIComponent(location.href);window.open('http://HOST:8090/add?url='+u,'_blank');})();
```

Or use **Site grabber** in the UI to pull every link from a page.

## Architecture

```
engine.py   — Task / Segment / RateLimiter / DownloadManager
              • probe (HEAD + Range) → size, resume support, filename
              • preallocate file, one worker thread per connection
              • dynamic split of the largest remaining segment
              • SQLite persistence + queue scheduler
app.py      — FastAPI JSON API + static hosting + site grabber
static/index.html — the web UI (vanilla JS, 1s polling)
```

### How dynamic segmentation works

1. Start one connection on the whole file.
2. When a connection frees up (or a worker finishes), find the segment with the **largest remaining byte range**.
3. Split it **in half**; the new connection starts on the second half.
4. Never split below 2 MB (`MIN_SEGMENT`).
5. Repeat up to `max_conn` connections.

### Notes

- Files are written with `os.pwrite` at exact offsets, so segments never collide.
- `Accept-Encoding: identity` is always sent so servers return raw bytes (no gzip truncation).
- The database is `downloads.db` in this directory; delete it to start fresh.
