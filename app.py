"""IDM-like download manager — web server and API."""
import os
import re
import sys
import html as htmllib
import urllib.parse
import threading
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import DownloadManager, UA

HERE = os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """Resolve bundled data files when frozen with PyInstaller."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(HERE, rel)


STATIC_DIR = resource_path("static")
app = FastAPI(title="IDM-like Download Manager", version="1.0.0")
manager = DownloadManager()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AddRequest(BaseModel):
    url: str
    filename: Optional[str] = None
    path: Optional[str] = None
    category: Optional[str] = None
    max_conn: Optional[int] = None
    speed_limit: Optional[int] = None
    start: Optional[bool] = True


class SettingsRequest(BaseModel):
    download_dir: Optional[str] = None
    max_concurrent: Optional[int] = None
    max_conn: Optional[int] = None
    global_speed_limit: Optional[int] = None


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/downloads")
def list_downloads():
    return manager.list_stats()


@app.post("/api/downloads")
def add(req: AddRequest):
    try:
        t = manager.add(req.url, req.filename, req.path, req.category,
                        req.max_conn, req.speed_limit, req.start)
        return manager.stats(t)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/downloads/{task_id}/pause")
def pause(task_id: str):
    t = manager.pause(task_id)
    if not t:
        raise HTTPException(404, "download not found")
    return manager.stats(t)


@app.post("/api/downloads/{task_id}/resume")
def resume(task_id: str):
    t = manager.resume(task_id)
    if not t:
        raise HTTPException(404, "download not found")
    return manager.stats(t)


@app.post("/api/downloads/{task_id}/cancel")
def cancel(task_id: str):
    t = manager.cancel(task_id)
    if not t:
        raise HTTPException(404, "download not found")
    return manager.stats(t)


@app.delete("/api/downloads/{task_id}")
def delete(task_id: str):
    manager.delete_task(task_id)
    return {"ok": True}


@app.post("/api/downloads/{task_id}/speed")
def set_speed(task_id: str, body: dict):
    t = manager.tasks.get(task_id)
    if not t:
        raise HTTPException(404, "download not found")
    t.speed_limit = int(body.get("speed_limit", 0) or 0)
    from engine import RateLimiter
    t.limiter = RateLimiter(t.speed_limit) if t.speed_limit else None
    manager.save_task(t)
    return manager.stats(t)


@app.get("/api/settings")
def get_settings():
    return {
        "download_dir": manager.download_dir,
        "max_concurrent": manager.max_concurrent,
        "max_conn": int(manager.get_setting("max_conn", 8)),
        "global_speed_limit": int(manager.get_setting("global_speed_limit", 0)),
    }


@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    if req.download_dir:
        manager.download_dir = req.download_dir
        os.makedirs(req.download_dir, exist_ok=True)
        manager.set_setting("download_dir", req.download_dir)
    if req.max_concurrent:
        manager.max_concurrent = req.max_concurrent
        manager.set_setting("max_concurrent", req.max_concurrent)
    if req.max_conn:
        manager.set_setting("max_conn", req.max_conn)
    if req.global_speed_limit is not None:
        manager.set_setting("global_speed_limit", req.global_speed_limit)
    return get_settings()


@app.post("/api/capture")
def capture(body: dict):
    """Endpoint used by the browser bookmarklet / extension."""
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    t = manager.add(url, filename=body.get("filename"),
                    category=body.get("category"))
    return manager.stats(t)


@app.post("/api/grab")
def grab(body: dict):
    """Site grabber: fetch a page and return matching links."""
    url = (body.get("url") or "").strip()
    pattern = body.get("pattern", "")
    if not url:
        raise HTTPException(400, "url required")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    base = urllib.parse.urlparse(url)
    links = []
    seen = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', r.text, re.I):
        raw = htmllib.unescape(m.group(1)).strip()
        if raw.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urllib.parse.urljoin(url, raw)
        p = urllib.parse.urlparse(full)
        if p.scheme not in ("http", "https"):
            continue
        if pattern and not re.search(pattern, full, re.I):
            continue
        if full in seen:
            continue
        seen.add(full)
        links.append({"url": full, "name": os.path.basename(p.path) or full})
    return {"count": len(links), "links": links}


@app.post("/api/grab/add")
def grab_add(body: dict):
    """Add all matching links from a page to the queue."""
    result = grab(body)
    added = []
    for link in result["links"]:
        t = manager.add(link["url"], filename=body.get("filename"),
                        category=body.get("category"))
        added.append(t.id)
    return {"added": len(added), "ids": added}


@app.get("/api/files")
def files():
    out = []
    for root, _, names in os.walk(manager.download_dir):
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
                out.append({"name": n, "path": p, "size": st.st_size,
                            "mtime": st.st_mtime})
            except OSError:
                pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


@app.get("/api/file/{task_id}")
def serve(task_id: str):
    t = manager.tasks.get(task_id)
    if not t:
        raise HTTPException(404, "not found")
    path = os.path.join(t.path, t.filename)
    if not os.path.exists(path):
        raise HTTPException(404, "file missing")
    return FileResponse(path, filename=t.filename)


@app.get("/api/bookmarklet")
def bookmarklet():
    return {"code": "javascript:(function(){var u=encodeURIComponent(location.href);"
                    "window.open('http://HOST:PORT/add?url='+u,'_blank');})();"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("IDM_PORT", "8090")))
