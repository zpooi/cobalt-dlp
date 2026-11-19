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
# 是否强制把视频转码成 H.264/AAC 的 mp4（iOS/微信/相册兼容）。默认开启。
MOBILE_SAFE = os.environ.get("MOBILE_SAFE", "1").lower() not in ("0", "false", "no", "off")
# 转码时限制最大高度（例如 1080）；0 = 保持原分辨率。手机播放 4K 会卡，可设为 1080。
TRANSCODE_MAX_HEIGHT = int(os.environ.get("TRANSCODE_MAX_HEIGHT", "0") or 0)

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
    # 优先 avc1(H.264)+m4a 的 mp4 组合，这是 iPhone/Safari/相册原生支持的编码；
    # 拿不到再用通用组合兜底，最后统一转成 H.264+AAC 的 mp4（见 run_job）。
    "best": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=avc1]+ba/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "1080": "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=720][vcodec^=avc1]+ba/bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=480][vcodec^=avc1]+ba/bv*[height<=480][ext=mp4]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "bestaudio/best",
}


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", s)
    return s[:120].strip("_") or "file"


FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# iPhone/Safari/相册(QuickTime) 真正能播放的编码组合。
# 注意：VP9/AV1 即使装在 .mp4 里，iOS 也不认 —— 表现就是黑屏、只有声音、
# 或者 QQ/微信弹"不支持的格式"。HEVC(H.265) 相册能放但 QQ/微信不稳，统一转掉。
APPLE_SAFE_VIDEO = ("h264",)
APPLE_SAFE_PIXFMT = ("yuv420p", "yuvj420p", "nv12")
APPLE_SAFE_AUDIO = ("aac", "mp3")
APPLE_BAD_H264_PROFILES = (
    "High 10", "High 4:2:2", "High 4:4:4", "High 10 Intra",
    "High 4:2:2 Intra", "High 4:4:4 Intra", "CAVLC 4:4:4 Intra",
)
APPLE_MAX_H264_LEVEL = 52  # 5.2，覆盖到 4K60；再高 iPhone 解不了


def _job_note(job, line):
    """Log a human-readable note both to stdout and to the job record."""
    print(f"[cobalt-dlp] {line}")
    if job is None:
        return
    with JOBS_LOCK:
        notes = job.setdefault("notes", [])
        notes.append(line)
        del notes[:-20]


def probe_media(path):
    """Describe the first real video/audio stream.

    Returns {} when ffprobe is missing or the file can't be read, so callers
    can tell "clean" apart from "couldn't check" (never assume clean).
    """
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
    except FileNotFoundError:
        print("[cobalt-dlp] ffprobe 未安装，无法检测编码")
        return {}
    except Exception as e:  # noqa
        print(f"[cobalt-dlp] ffprobe failed: {e}")
        return {}

    streams = data.get("streams") or []
    vid = None
    for st in streams:
        if st.get("codec_type") != "video":
            continue
        # 封面图(attached_pic)不是正片，跳过，否则会误判成"已是 H.264"
        if (st.get("disposition") or {}).get("attached_pic"):
            continue
        vid = st
        break
    aud = next((st for st in streams if st.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "format": fmt.get("format_name") or "",
        "duration": duration or None,
        "has_video": vid is not None,
        "has_audio": aud is not None,
        "vcodec": (vid or {}).get("codec_name"),
        "vprofile": (vid or {}).get("profile"),
        "vpix": (vid or {}).get("pix_fmt"),
        "vlevel": (vid or {}).get("level"),
        "width": (vid or {}).get("width"),
        "height": (vid or {}).get("height"),
        "acodec": (aud or {}).get("codec_name"),
        "aprofile": (aud or {}).get("profile"),
    }


def apple_incompat_reason(info):
    """Return why iOS/相册/QQ can't play this file, or None if it's fine."""
    if not info:
        return "无法读取编码信息"
    fmt = info.get("format") or ""
    if fmt and not any(x in fmt for x in ("mp4", "mov", "m4a", "3gp", "mj2")):
        return f"容器 {fmt} 不是 mp4"
    if not info.get("has_video"):
        return None  # 纯音频，不归这里管
    v = info.get("vcodec")
    if v not in APPLE_SAFE_VIDEO:
        return f"视频编码是 {v}，iOS/Safari 不支持（装在 mp4 里也不行）"
    prof = info.get("vprofile") or ""
    if any(p in prof for p in APPLE_BAD_H264_PROFILES):
        return f"H.264 {prof}（非 8-bit 4:2:0）iOS 不支持"
    pix = info.get("vpix") or ""
    if pix and pix not in APPLE_SAFE_PIXFMT:
        return f"像素格式 {pix} iOS 不支持"
    lvl = info.get("vlevel") or 0
    if lvl > APPLE_MAX_H264_LEVEL:
        return f"H.264 level {lvl / 10:.1f} 超出 iOS 支持范围"
    a = info.get("acodec")
    if a is not None and a not in APPLE_SAFE_AUDIO:
        return f"音频编码是 {a}，iOS/微信支持不佳"
    return None


def ensure_apple_playable(video_path, job=None):
    """Normalise a video to H.264 (8-bit 4:2:0) + AAC-LC in a faststart mp4.

    yt-dlp happily merges VP9/AV1 streams into an .mp4 container; desktop
    players decode those but iPhone/Safari/相册 show a black screen with
    audio (or refuse to save). We probe the real streams and re-encode when
    anything isn't natively playable, then verify the result.
    Returns the (possibly new) path to use.
    """
    info = probe_media(video_path)
    if not info:
        _job_note(job, "无法探测编码（ffprobe 不可用？），跳过转码")
        return video_path
    if not info.get("has_video"):
        return video_path  # 纯音频 / 图片

    reason = apple_incompat_reason(info)
    if reason is None:
        _job_note(job, f"已是 H.264/AAC（{info.get('width')}x{info.get('height')}），无需转码")
        return video_path

    _job_note(job, f"转码为 H.264/AAC：{reason}")

    base, _ext = os.path.splitext(video_path)
    tmp = base + ".__iosfix.mp4"
    out_path = base + ".mp4"

    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # 保证宽高是偶数(H.264 要求)
    if TRANSCODE_MAX_HEIGHT > 0:
        h = TRANSCODE_MAX_HEIGHT
        vf = (f"scale=w='if(gt(ih,{h}),-2,iw)':h='min(ih,{h})'," + vf)

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", video_path,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-sn", "-dn",
           "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
           "-crf", "23", "-pix_fmt", "yuv420p", "-vf", vf]
    if info.get("has_audio"):
        # AAC-LC + 立体声，手机/微信/QQ 全部认
        cmd += ["-c:a", "aac", "-profile:a", "aac_low",
                "-ar", "44100", "-ac", "2", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", tmp]

    # 时长越长给的时间越宽，避免长视频被 3600s 卡死
    timeout = 1800
    if info.get("duration"):
        timeout = max(600, min(7200, int(info["duration"] * 30)))

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        err = (r.stderr or "").strip()
        if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            _job_note(job, "转码失败：" + (err.splitlines()[-1] if err else f"ffmpeg exit {r.returncode}"))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return video_path

        # 转完必须复检，别把"转失败但生成了文件"当成成功
        check = probe_media(tmp)
        bad = apple_incompat_reason(check)
        if bad is not None:
            _job_note(job, "转码结果仍不兼容：" + bad)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return video_path

        os.replace(tmp, out_path)
        if os.path.abspath(out_path) != os.path.abspath(video_path):
            try:
                os.remove(video_path)  # 删掉原文件(如 .webm)
            except OSError:
                pass
        _job_note(job, f"转码完成：{os.path.getsize(out_path) // 1024} KB，"
                       f"{check.get('vcodec')}/{check.get('acodec')}")
        return out_path
    except subprocess.TimeoutExpired:
        _job_note(job, f"转码超时（>{timeout}s），保留原文件")
    except FileNotFoundError:
        _job_note(job, "未安装 ffmpeg，无法转码")
    except Exception as e:  # noqa
        _job_note(job, f"转码出错：{e}")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return video_path


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
    # 视频统一成 mp4；H.264 兼容性由下载后的 ensure_apple_playable() 兜底转码保证。
    if job.get("quality") == "audio":
        cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["--merge-output-format", "mp4"]
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
            # 保证视频是 H.264/AAC 的 mp4（iPhone/Safari 能播放和保存）
            if MOBILE_SAFE and job.get("quality") != "audio" and \
                    name.lower().endswith((".mp4", ".m4v", ".mkv", ".webm", ".mov")):
                with JOBS_LOCK:
                    job["phase"] = "converting"
                full = ensure_apple_playable(full, job)
                name = os.path.basename(full)
            if not os.path.isfile(full) or os.path.getsize(full) == 0:
                continue
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


def download_filename(fname):
    """Build an ASCII download name that ALWAYS keeps the real extension.

    Old code stripped non-ASCII chars and then .strip("_"), so a Chinese-only
    title like "视频.mp4" became ".mp4" — a hidden file with no name, which
    iOS Files / QQ show as "无法识别的格式". Never let that happen.
    """
    stem, ext = os.path.splitext(fname)
    ext = (ext or "").lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".mp4"
    ascii_stem = re.sub(r"[^A-Za-z0-9._\-]+", "_", stem).strip("._-")
    ascii_stem = re.sub(r"_{2,}", "_", ascii_stem)[:150].strip("._-") or "video"
    return ascii_stem + ext


class Handler(BaseHTTPRequestHandler):
    server_version = "cobalt-dlp/1.1"
    # iOS 的 AVPlayer 需要 HTTP/1.1 的 keep-alive + 精确 Range 支持才肯流式播放
    protocol_version = "HTTP/1.1"
    # keep-alive 空闲连接太久就关掉，免得线程一直挂着
    timeout = 120
    _head_only = False

    def do_HEAD(self):
        self._head_only = True
        self.do_GET()

    # ---- helpers ----
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not self._head_only:
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
            ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
            return self._send_json({"ok": True, "yt_dlp": yt_dlp_version(),
                                    "engine": " ".join(YTDLP),
                                    "ffmpeg": ffmpeg_ok,
                                    "mobile_safe": MOBILE_SAFE,
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
                "notes": [],
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
        if not self._head_only:
            self.wfile.write(data)

    def _parse_range(self, size):
        """Parse a single-range header. Returns (start, end), None, or 'invalid'."""
        rng = (self.headers.get("Range") or "").strip()
        if not rng.lower().startswith("bytes="):
            return None
        spec = rng[6:].split(",")[0].strip()
        if "-" not in spec:
            return None
        s, e = spec.split("-", 1)
        try:
            if s == "":
                n = int(e)                       # bytes=-500 -> 最后 500 字节
                if n <= 0:
                    return "invalid"
                return max(0, size - n), size - 1
            start = int(s)
            end = int(e) if e else size - 1
        except ValueError:
            return None
        if start < 0 or start >= size or start > end:
            return "invalid"
        return start, min(end, size - 1)

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
        low = full.lower()
        ctype = "application/octet-stream"
        if low.endswith((".mp4", ".m4v")):
            ctype = "video/mp4"
        elif low.endswith(".mov"):
            ctype = "video/quicktime"
        elif low.endswith(".webm"):
            ctype = "video/webm"
        elif low.endswith(".mkv"):
            ctype = "video/x-matroska"
        elif low.endswith(".mp3"):
            ctype = "audio/mpeg"
        elif low.endswith((".m4a", ".aac")):
            ctype = "audio/mp4"
        elif low.endswith((".opus", ".ogg")):
            ctype = "audio/ogg"
        elif low.endswith((".jpg", ".jpeg")):
            ctype = "image/jpeg"
        elif low.endswith(".png"):
            ctype = "image/png"
        elif low.endswith(".webp"):
            ctype = "image/webp"

        # iOS AVPlayer 会先用小 Range 探测，处理不对就整段放弃(黑屏)
        r = self._parse_range(size)
        if r == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return
        partial = r is not None
        start, end = r if r else (0, max(0, size - 1))
        length = max(0, end - start + 1)

        fname = parts[1]
        ascii_name = download_filename(fname)
        if parse_qs(urlparse(self.path).query).get("dl"):
            # 同时给 ASCII 名和 UTF-8 名：filename= 保证任何客户端都能存，
            # filename*= 让 iOS/Chrome 显示原始(可能含中文)标题。
            from urllib.parse import quote
            disp = ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
                    % (ascii_name, quote(fname)))
        else:
            # 内联播放，明确 inline，避免被当成附件下载
            disp = 'inline; filename="%s"' % ascii_name

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", disp)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if self._head_only or length == 0:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    print(f"[cobalt-dlp] engine: {' '.join(YTDLP)}  version: {yt_dlp_version()}")
    if MOBILE_SAFE and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("[cobalt-dlp] 警告: 未找到 ffmpeg/ffprobe，无法把视频转成 iPhone 可播放的 H.264/AAC！")
        print("[cobalt-dlp]        Docker: 镜像已自带；直接运行请先 apt install ffmpeg")
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
