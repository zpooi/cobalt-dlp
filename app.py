#!/usr/bin/env python3
"""
cobalt-dlp — a small web downloader powered by yt-dlp.

Paste a link -> server downloads it with yt-dlp -> play / download from the page.

Supports optional cookies (cookies.txt) for Instagram / YouTube / etc.

Endpoints:
  GET  /                -> frontend page
  POST /api/fetch       -> {url, quality?}  start a job, returns {id}
  GET  /api/jobs        -> list all jobs
  GET  /api/job/<id>    -> one job status (+progress)
  GET  /files/<name>    -> download/stream a finished file
  DELETE /api/job/<id>  -> delete a job and its file
  GET  /api/health      -> {ok, yt_dlp_version}

Files are kept for RETENTION_HOURS (default 24) and then auto-deleted
by a background janitor thread, so they never pile up on the server.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(HERE, "downloads"))
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(HERE, "cookies.txt"))
PORT = int(os.environ.get("PORT", "13000"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_JOBS = int(os.environ.get("MAX_JOBS", "200"))
# 文件在服务器上保留的时长（小时），到期由后台清理线程自动删除，默认 24 小时即 1 天。
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
# 清理线程的扫描间隔（秒）。默认 10 分钟扫一次。
CLEANUP_INTERVAL = float(os.environ.get("CLEANUP_INTERVAL", "600"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

JOBS = {}            # id -> dict
JOBS_LOCK = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def purge_job_files(job_id):
    """Remove a job's directory from disk (best-effort)."""
    d = os.path.join(DOWNLOAD_DIR, job_id)
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def cleanup_expired():
    """Delete jobs (files + records) older than RETENTION_HOURS.

    A job's age is measured from when it finished (or was created, if it
    never finished). Running/queued jobs whose download is still going are
    left alone so a slow download is never killed mid-flight.
    """
    if RETENTION_HOURS <= 0:
        return
    cutoff = now_ms() - int(RETENTION_HOURS * 3600 * 1000)
    expired = []
    with JOBS_LOCK:
        for jid, job in list(JOBS.items()):
            if job.get("status") in ("queued", "running"):
                continue
            stamp = job.get("finished") or job.get("created") or 0
            if stamp and stamp < cutoff:
                expired.append(jid)
        for jid in expired:
            JOBS.pop(jid, None)
    for jid in expired:
        purge_job_files(jid)
    if expired:
        print(f"[cobalt-dlp] cleaned up {len(expired)} expired job(s) "
              f"(older than {RETENTION_HOURS:g}h)")


def janitor_loop():
    while True:
        try:
            cleanup_expired()
        except Exception as e:  # noqa
            print(f"[cobalt-dlp] cleanup error: {e}")
        time.sleep(max(5.0, CLEANUP_INTERVAL))


def yt_dlp_version():
    try:
        out = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or out.stderr.strip()
    except Exception as e:  # noqa
        return f"error: {e}"


def find_yt_dlp():
    """Return a command list that runs yt-dlp."""
    # Prefer module form so it works in venv/containers.
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    return ["yt-dlp"]


YTDLP = find_yt_dlp()


QUALITY_FMT = {
    # label -> yt-dlp -f selector
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "bestaudio/best",
}


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", s)
    return s[:120].strip("_") or "file"


def run_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return

    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    fmt = QUALITY_FMT.get(job.get("quality", "best"), QUALITY_FMT["best"])
    outtmpl = os.path.join(job_dir, "%(title).80s.%(ext)s")

    cmd = YTDLP + [
        "-f", fmt,
        "--no-playlist",
        "--no-overwrites",
        "--restrict-filenames",
        "--newline",
        "--no-color",
        "-o", outtmpl,
    ]
    if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        cmd += ["--cookies", COOKIES_FILE]
    cmd += [job["url"]]

    with JOBS_LOCK:
        job["status"] = "running"
        job["updated"] = now_ms()

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail = []
    try:
        for line in proc.stdout:  # type: ignore
            line = line.rstrip("\n")
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                with JOBS_LOCK:
                    job["progress"] = float(m.group(1))
            # merge/extract stage
            if "Merging formats" in line or "Extracting audio" in line:
                with JOBS_LOCK:
                    job["phase"] = "merging"
        proc.wait()
    except Exception as e:  # noqa
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = str(e)
            job["log"] = "\n".join(tail)
            job["finished"] = now_ms()
            job["updated"] = now_ms()
        return

    if proc.returncode != 0:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "yt-dlp exited with %s" % proc.returncode
            job["log"] = "\n".join(tail)
            job["finished"] = now_ms()
            job["updated"] = now_ms()
        return

    # collect produced files (skip .part / .ytdl)
    files = []
    for name in sorted(os.listdir(job_dir)):
        if name.endswith(".part") or name.endswith(".ytdl") or name.endswith(".temp"):
            continue
        full = os.path.join(job_dir, name)
        if os.path.isfile(full) and os.path.getsize(full) > 0:
            files.append({"name": name, "size": os.path.getsize(full),
                          "rel": job_id + "/" + name})

    if not files:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "no output file produced"
            job["log"] = "\n".join(tail)
            job["finished"] = now_ms()
            job["updated"] = now_ms()
        return

    with JOBS_LOCK:
        job["status"] = "done"
        job["progress"] = 100.0
        job["files"] = files
        job["filename"] = files[0]["name"]
        job["finished"] = now_ms()
        job["updated"] = now_ms()


class Handler(BaseHTTPRequestHandler):
    server_version = "cobalt-dlp/1.0"

    # ---- helpers ----
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _public_job(self, job):
        """Copy a job for JSON output and attach retention/expiry info."""
        out = dict(job)
        if RETENTION_HOURS > 0 and job.get("status") in ("done", "error"):
            stamp = job.get("finished") or job.get("created") or now_ms()
            out["expires"] = stamp + int(RETENTION_HOURS * 3600 * 1000)
        else:
            out["expires"] = None
        return out

    # ---- routing ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/health":
            return self._send_json({"ok": True, "yt_dlp": yt_dlp_version(),
                                    "engine": " ".join(YTDLP),
                                    "retention_hours": RETENTION_HOURS})
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                jobs = [self._public_job(j) for j in jobs]
            return self._send_json({"jobs": jobs, "retention_hours": RETENTION_HOURS})
        m = re.match(r"^/api/job/([A-Za-z0-9_\-]+)$", path)
        if m:
            with JOBS_LOCK:
                job = JOBS.get(m.group(1))
            if not job:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(self._public_job(job))
        if path.startswith("/files/"):
            return self._serve_file(unquote(path[len("/files/"):]))
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/fetch":
            body = self._read_json()
            url = (body.get("url") or "").strip()
            quality = (body.get("quality") or "best").strip()
            if not url or not re.match(r"^https?://", url):
                return self._send_json({"error": "invalid url"}, 400)
            if quality not in QUALITY_FMT:
                quality = "best"
            job_id = uuid.uuid4().hex[:16]
            job = {
                "id": job_id,
                "url": url,
                "quality": quality,
                "status": "queued",
                "progress": 0.0,
                "phase": "downloading",
                "created": now_ms(),
                "updated": now_ms(),
                "files": [],
                "error": None,
            }
            with JOBS_LOCK:
                if len(JOBS) > MAX_JOBS:
                    # drop oldest finished
                    done = sorted([j for j in JOBS.values() if j["status"] in ("done", "error")],
                                  key=lambda j: j["created"])
                    for j in done[:max(0, len(JOBS) - MAX_JOBS + 10)]:
                        JOBS.pop(j["id"], None)
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
            return self._send_json({"id": job_id, "job": job})
        return self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        m = re.match(r"^/api/job/([A-Za-z0-9_\-]+)$", parsed.path)
        if not m:
            return self._send_json({"error": "not found"}, 404)
        jid = m.group(1)
        with JOBS_LOCK:
            JOBS.pop(jid, None)
        # remove the job's whole directory (files + any residue)
        purge_job_files(jid)
        return self._send_json({"ok": True})

    # ---- static ----
    def _serve_index(self):
        idx = os.path.join(HERE, "index.html")
        try:
            with open(idx, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._send_json({"error": "index.html missing"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, rel):
        # rel like "<jobid>/<name>"
        parts = rel.split("/")
        if len(parts) != 2 or ".." in parts:
            return self._send_json({"error": "bad path"}, 400)
        full = os.path.join(DOWNLOAD_DIR, parts[0], parts[1])
        full = os.path.abspath(full)
        base = os.path.abspath(DOWNLOAD_DIR)
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self._send_json({"error": "not found"}, 404)
        size = os.path.getsize(full)
        ctype = "application/octet-stream"
        if full.lower().endswith((".mp4", ".m4v", ".webm", ".mkv")):
            ctype = "video/mp4" if full.lower().endswith((".mp4", ".m4v")) else "video/webm"
        elif full.lower().endswith((".mp3", ".m4a", ".opus", ".ogg")):
            ctype = "audio/mpeg"
        elif full.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            ctype = "image/" + full.rsplit(".", 1)[-1].lower()

        # support Range for video seeking
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            partial = True
            spec = rng[len("bytes="):].split(",")[0].strip()
            if "-" in spec:
                s, e = spec.split("-", 1)
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                if not e and s:
                    end = size - 1
                start = max(0, start)
                end = min(end, size - 1)
        length = end - start + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # let browser download with filename
        if parse_qs(urlparse(self.path).query).get("dl"):
            fname = parts[1]
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    print(f"[cobalt-dlp] engine: {' '.join(YTDLP)}  version: {yt_dlp_version()}")
    print(f"[cobalt-dlp] listening on http://{HOST}:{PORT}  downloads -> {DOWNLOAD_DIR}")
    if RETENTION_HOURS > 0:
        print(f"[cobalt-dlp] files kept for {RETENTION_HOURS:g}h "
              f"(scan every {CLEANUP_INTERVAL:g}s), then auto-deleted")
    else:
        print("[cobalt-dlp] retention disabled (RETENTION_HOURS=0), files kept forever")
    # 启动时先清一次，把上次进程遗留的过期文件清掉，再开启后台定时清理。
    try:
        cleanup_expired()
    except Exception as e:  # noqa
        print(f"[cobalt-dlp] startup cleanup error: {e}")
    threading.Thread(target=janitor_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[cobalt-dlp] bye")


if __name__ == "__main__":
    main()
