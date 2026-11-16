"""IDM-style download engine.

Features:
- Dynamic file segmentation (IDM's in-half division rule)
- Resume via HTTP Range requests, progress persisted to SQLite
- Pause / resume / cancel, download queue
- Per-task and global speed limits
- Categories and default directories
"""
import os
import re
import json
import time
import threading
import sqlite3
import uuid
import urllib.parse
import mimetypes

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CHUNK = 262144               # 256 KB
MIN_SEGMENT = 2 * 1024 * 1024  # don't split below 2 MB
# download managers must fetch the raw bytes, never a decompressed stream
BASE_HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
DEFAULT_MAX_CONN = 8
HERE = os.path.dirname(os.path.abspath(__file__))


def default_db_path():
    """A user-writable database location (Windows-safe)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "IDM-Like")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = os.path.expanduser("~")
        return os.path.join(d, "downloads.db")
    return os.path.join(HERE, "downloads.db")


class RateLimiter:
    """Shared token bucket so all segments of a task respect one speed limit."""

    def __init__(self, rate):
        self.rate = float(rate)
        self.tokens = 0.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n):
        if self.rate <= 0:
            return
        remaining = float(n)
        while remaining > 0:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate,
                                  self.tokens + (now - self.last) * self.rate)
                self.last = now
                take = min(self.tokens, remaining)
                self.tokens -= take
                remaining -= take
                if remaining <= 0:
                    return
                wait = min(remaining / self.rate, 0.5)
            time.sleep(wait)


class Segment:
    __slots__ = ("start", "end", "downloaded", "done", "assigned", "error")

    def __init__(self, start, end):
        self.start = start
        self.end = end          # inclusive
        self.downloaded = 0
        self.done = False
        self.assigned = False
        self.error = None

    def remaining(self):
        return self.end - (self.start + self.downloaded) + 1

    def to_dict(self):
        return {"start": self.start, "end": self.end,
                "downloaded": self.downloaded, "done": self.done,
                "assigned": self.assigned, "error": self.error}

    @classmethod
    def from_dict(cls, d):
        s = cls(d["start"], d["end"])
        s.downloaded = d.get("downloaded", 0)
        s.done = d.get("done", False)
        s.assigned = d.get("assigned", False)
        s.error = d.get("error")
        return s


class Task:
    def __init__(self, url, filename=None, path=None, category=None,
                 max_conn=DEFAULT_MAX_CONN, speed_limit=0):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.filename = filename
        self.path = path
        self.category = category
        self.max_conn = max_conn
        self.speed_limit = speed_limit      # bytes/sec, 0 = unlimited
        self.total = 0
        self.downloaded = 0
        self.status = "queued"              # queued/running/paused/completed/error/canceled
        self.segments = []
        self.workers = []
        self.lock = threading.RLock()
        self.fd = None
        self.target_path = None
        self._handles = {}
        self.error = None
        self.supports_range = True
        self.created_at = time.time()
        self.started_at = None
        self.completed_at = None
        self.speed = 0.0
        self._stop = threading.Event()
        self._manager = None
        self._save_timer = 0
        self._speed_window = []
        self.limiter = None

    # ---------- serialization ----------
    def to_dict(self):
        return {
            "id": self.id, "url": self.url, "filename": self.filename,
            "path": self.path, "category": self.category,
            "total": self.total, "downloaded": self.downloaded,
            "status": self.status, "max_conn": self.max_conn,
            "speed_limit": self.speed_limit, "speed": round(self.speed, 1),
            "segments": [s.to_dict() for s in self.segments],
            "supports_range": self.supports_range,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def save_dict(self):
        return {
            "id": self.id, "url": self.url, "filename": self.filename,
            "path": self.path, "category": self.category,
            "total": self.total, "downloaded": self.downloaded,
            "status": self.status, "max_conn": self.max_conn,
            "speed_limit": self.speed_limit, "supports_range": self.supports_range,
            "segments": json.dumps([s.to_dict() for s in self.segments]),
            "error": self.error,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class DownloadManager:
    def __init__(self, db_path=None, download_dir=None, max_concurrent=3):
        self.db_path = db_path or default_db_path()
        self.download_dir = download_dir or os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(self.download_dir, exist_ok=True)
        self.max_concurrent = max_concurrent
        self.tasks = {}
        self.lock = threading.RLock()
        self._db_lock = threading.Lock()
        self._init_db()
        self._load()
        self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True)
        self._queue_thread.start()

    # ---------- database ----------
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_db(self):
        with self._db_lock, self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, url TEXT, filename TEXT, path TEXT,
                category TEXT, total INTEGER, downloaded INTEGER, status TEXT,
                max_conn INTEGER, speed_limit INTEGER, supports_range INTEGER,
                segments TEXT, error TEXT, created_at REAL, started_at REAL,
                completed_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT)""")

    def _load(self):
        with self._db_lock, self._conn() as c:
            rows = c.execute("SELECT * FROM tasks").fetchall()
        cols = ["id", "url", "filename", "path", "category", "total",
                "downloaded", "status", "max_conn", "speed_limit",
                "supports_range", "segments", "error", "created_at",
                "started_at", "completed_at"]
        for row in rows:
            d = dict(zip(cols, row))
            t = Task(d["url"], d["filename"], d["path"], d["category"],
                     d["max_conn"], d["speed_limit"])
            t.id = d["id"]
            t.total = d["total"] or 0
            t.downloaded = d["downloaded"] or 0
            t.status = d["status"]
            t.supports_range = bool(d["supports_range"])
            t.error = d["error"]
            t.created_at = d["created_at"] or time.time()
            t.started_at = d["started_at"]
            t.completed_at = d["completed_at"]
            try:
                t.segments = [Segment.from_dict(x) for x in json.loads(d["segments"] or "[]")]
            except Exception:
                t.segments = []
            # a task that was running when the app stopped is paused
            if t.status == "running":
                t.status = "paused"
            if t.status in ("queued",):
                t.status = "paused"
            with self.lock:
                self.tasks[t.id] = t

    def save_task(self, t):
        with self._db_lock, self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO tasks
                (id,url,filename,path,category,total,downloaded,status,max_conn,
                 speed_limit,supports_range,segments,error,created_at,started_at,completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t.id, t.url, t.filename, t.path, t.category, t.total,
                 t.downloaded, t.status, t.max_conn, t.speed_limit,
                 int(t.supports_range), json.dumps([s.to_dict() for s in t.segments]),
                 t.error, t.created_at, t.started_at, t.completed_at))

    def delete_task(self, task_id):
        with self.lock:
            t = self.tasks.pop(task_id, None)
        if t:
            self._stop_task(t)
        with self._db_lock, self._conn() as c:
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def get_setting(self, key, default=None):
        with self._db_lock, self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self._db_lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    # ---------- queue ----------
    def _queue_loop(self):
        while True:
            try:
                with self.lock:
                    running = [t for t in self.tasks.values() if t.status == "running"]
                    slots = self.max_concurrent - len(running)
                    if slots > 0:
                        queued = [t for t in self.tasks.values() if t.status == "queued"]
                        for t in queued[:slots]:
                            self._start(t)
            except Exception:
                pass
            time.sleep(1.0)

    # ---------- lifecycle ----------
    def add(self, url, filename=None, path=None, category=None,
            max_conn=None, speed_limit=None, start=True):
        if not url:
            raise ValueError("URL is required")
        with self.lock:
            t = Task(url, filename=filename, path=path, category=category,
                     max_conn=max_conn or DEFAULT_MAX_CONN,
                     speed_limit=speed_limit or 0)
            self.tasks[t.id] = t
            self.save_task(t)
            if start:
                t.status = "queued"
                self.save_task(t)
            return t

    def _start(self, t):
        t.status = "running"
        t.started_at = t.started_at or time.time()
        t._stop.clear()
        self.save_task(t)
        threading.Thread(target=self._prepare, args=(t,), daemon=True).start()

    def resume(self, task_id):
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                return None
            if t.status in ("paused", "error", "canceled", "completed"):
                if t.status == "completed":
                    return t
                t.status = "queued"
                t.error = None
                t._stop.clear()
                self.save_task(t)
            return t

    def pause(self, task_id):
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                return None
            if t.status == "running":
                t.status = "paused"
                t._stop.set()
                self._join_workers(t)
                self.save_task(t)
            return t

    def cancel(self, task_id):
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                return None
            t.status = "canceled"
            t._stop.set()
            self._join_workers(t)
            self.save_task(t)
            return t

    def _stop_task(self, t):
        t._stop.set()
        self._join_workers(t)
        for f in t._handles.values():
            try:
                f.close()
            except OSError:
                pass
        t._handles = {}
        try:
            if t.fd:
                os.close(t.fd)
                t.fd = None
        except OSError:
            pass

    def _join_workers(self, t):
        for w in list(t.workers):
            w.join(timeout=3)
        t.workers = []

    # ---------- preparation / probing ----------
    def _prepare(self, t):
        try:
            self._probe(t)
            if t.error:
                raise RuntimeError(t.error)
            if not t.filename:
                t.filename = self._guess_filename(t)
            if not t.path:
                t.path = self._category_dir(t.category)
            os.makedirs(t.path, exist_ok=True)
            target = os.path.join(t.path, t.filename)
            t.target_path = target
            # preallocate when the size is known
            if t.total > 0:
                with open(target, "ab") as f:
                    f.truncate(t.total)
            t.fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
            t.limiter = RateLimiter(t.speed_limit) if t.speed_limit else None
            # build segments
            with t.lock:
                if t.total > 0 and t.supports_range:
                    t.segments = [Segment(0, t.total - 1)]
                else:
                    t.supports_range = False
                    t.segments = [Segment(0, 2 ** 62)]
                    t.downloaded = 0
            t._manager = threading.Thread(target=self._manage, args=(t,), daemon=True)
            t._manager.start()
        except Exception as e:
            t.status = "error"
            t.error = str(e)
            self.save_task(t)

    def _probe(self, t):
        headers = dict(BASE_HEADERS)
        try:
            r = requests.head(t.url, headers=headers, allow_redirects=True, timeout=20)
            if r.status_code == 200:
                if r.headers.get("Content-Length"):
                    try:
                        t.total = int(r.headers["Content-Length"])
                    except ValueError:
                        pass
                if "bytes" in (r.headers.get("Accept-Ranges") or "").lower():
                    t.supports_range = True
                cd = r.headers.get("Content-Disposition") or ""
                m = re.search(r"filename\*?=(?:UTF-8'')?([^;]+)", cd, re.I)
                if m:
                    t.filename = urllib.parse.unquote(m.group(1).strip().strip('"'))
        except Exception:
            pass
        # range probe
        try:
            r2 = requests.get(t.url, headers={**headers, "Range": "bytes=0-0"},
                              stream=True, allow_redirects=True, timeout=20)
            code = r2.status_code
            if code == 206:
                t.supports_range = True
                if not t.total:
                    cr = r2.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)$", cr)
                    if m:
                        t.total = int(m.group(1))
            elif code == 200:
                t.supports_range = False
                if not t.total and r2.headers.get("Content-Length"):
                    t.total = int(r2.headers["Content-Length"])
            else:
                t.error = f"HTTP {code}"
            r2.close()
        except Exception as e:
            t.error = str(e)

    def _guess_filename(self, t):
        path = urllib.parse.urlparse(t.url).path
        name = os.path.basename(path) or "download"
        name = urllib.parse.unquote(name)
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "download"
        return name

    def _category_dir(self, category):
        dirs = {
            "video": "Videos", "music": "Music", "programs": "Programs",
            "documents": "Documents", "compressed": "Archives", "images": "Images",
        }
        sub = dirs.get((category or "").lower(), "")
        return os.path.join(self.download_dir, sub) if sub else self.download_dir

    # ---------- segmentation ----------
    def _manage(self, t):
        while not t._stop.is_set() and t.status == "running":
            with t.lock:
                active = sum(1 for w in t.workers if w.is_alive())
                free = t.max_conn - active
                # assign pending segments
                for seg in t.segments:
                    if free <= 0:
                        break
                    if not seg.assigned and not seg.done:
                        seg.assigned = True
                        w = threading.Thread(target=self._worker, args=(t, seg), daemon=True)
                        t.workers.append(w)
                        w.start()
                        free -= 1
                # dynamic split: divide the largest remaining segment in half
                if free > 0 and t.supports_range:
                    cands = [s for s in t.segments
                             if not s.done and s.remaining() > MIN_SEGMENT]
                    if cands:
                        biggest = max(cands, key=lambda s: s.remaining())
                        rem_start = biggest.start + biggest.downloaded
                        mid = rem_start + (biggest.end - rem_start) // 2
                        if mid > rem_start and mid < biggest.end:
                            old_end = biggest.end
                            biggest.end = mid
                            t.segments.append(Segment(mid + 1, old_end))
            if time.time() - t._save_timer > 3:
                self.save_task(t)
                t._save_timer = time.time()
            if t.supports_range and t.total and t.downloaded >= t.total:
                self._complete(t)
                return
            if not t.supports_range:
                with t.lock:
                    all_done = all(s.done for s in t.segments)
                    any_alive = any(w.is_alive() for w in t.workers)
                if all_done and not any_alive:
                    self._complete(t)
                    return
            time.sleep(0.4)

    def _write(self, t, offset, data):
        """Write bytes at an absolute offset (cross-platform)."""
        if hasattr(os, "pwrite"):
            os.pwrite(t.fd, data, offset)
            return
        # Windows fallback: one file handle per worker thread
        tid = threading.get_ident()
        f = t._handles.get(tid)
        if f is None:
            f = open(t.target_path, "r+b")
            t._handles[tid] = f
        f.seek(offset)
        f.write(data)

    def _worker(self, t, seg):
        while not t._stop.is_set() and t.status == "running":
            pos = seg.start + seg.downloaded
            if pos > seg.end:
                seg.done = True
                return
            headers = dict(BASE_HEADERS)
            headers["Range"] = f"bytes={pos}-{seg.end}"
            try:
                with requests.get(t.url, headers=headers, stream=True,
                                  allow_redirects=True, timeout=(10, 60)) as r:
                    if r.status_code == 200 and pos > 0:
                        # server ignored Range -> fall back to single connection
                        t.supports_range = False
                        seg.done = True
                        r.close()
                        return
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code}")
                    for chunk in r.iter_content(CHUNK):
                        if t._stop.is_set() or t.status != "running":
                            return
                        if not chunk:
                            continue
                        remaining = seg.end - (seg.start + seg.downloaded) + 1
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        if t.limiter:
                            t.limiter.consume(len(chunk))
                        self._write(t, seg.start + seg.downloaded, chunk)
                        seg.downloaded += len(chunk)
                        with t.lock:
                            t.downloaded += len(chunk)
                            t._speed_window.append((time.time(), len(chunk)))
                # stream finished: segment complete or whole body done
                seg.done = True
                return
            except Exception as e:
                seg.error = str(e)
                t.error = str(e)
                time.sleep(1.5)

    def _complete(self, t):
        with t.lock:
            if t.status != "running":
                return
            t.status = "completed"
            t.completed_at = time.time()
            if t.total:
                t.downloaded = t.total
            for s in t.segments:
                s.done = True
        self._stop_task(t)
        self.save_task(t)

    # ---------- stats ----------
    def stats(self, t):
        now = time.time()
        with t.lock:
            t._speed_window = [(ts, n) for ts, n in t._speed_window if now - ts < 3]
            total = sum(n for _, n in t._speed_window)
            t.speed = total / 3.0 if t.speed_window_len() else 0.0
            eta = 0
            if t.speed > 0 and t.total > t.downloaded:
                eta = int((t.total - t.downloaded) / t.speed)
            return {
                "id": t.id, "url": t.url, "filename": t.filename,
                "path": t.path, "category": t.category,
                "total": t.total, "downloaded": t.downloaded,
                "status": t.status, "speed": round(t.speed, 1),
                "max_conn": t.max_conn, "speed_limit": t.speed_limit,
                "segments": [s.to_dict() for s in t.segments],
                "supports_range": t.supports_range, "error": t.error,
                "eta": eta,
                "created_at": t.created_at,
                "started_at": t.started_at, "completed_at": t.completed_at,
            }

    def list_stats(self):
        with self.lock:
            return [self.stats(t) for t in self.tasks.values()]


# small helper attached to Task for speed window length
def _speed_window_len(self):
    return len(self._speed_window)

Task.speed_window_len = _speed_window_len
