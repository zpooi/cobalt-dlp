#!/usr/bin/env python3
"""
cobalt-dlp — a small web downloader powered by yt-dlp.

Paste a link -> server downloads it with yt-dlp -> play / download from the page.

Videos, audio, image posts (抖音/小红书/Instagram 图文) and direct image links
are all supported; multi-image jobs also get a zip bundle.

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

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from urllib.parse import (urlparse, parse_qs, unquote, quote, unquote_plus,
                          urlencode)

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(HERE, "downloads"))
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(HERE, "cookies.txt"))
# 目录里的所有 *.txt 都会被自动合并成一个 cookies 文件：抖音导出成
# www.douyin_com_cookies.txt、B站导出成 bilibili.txt 各放一份就行，不用手动拼接。
# 本地直接跑时 COOKIES_FILE 和代码在同一目录，不能把整个项目目录当 cookies 目录
# （会把 requirements.txt 也读进来），默认用 ./cookies。
_default_cookie_dir = os.path.dirname(COOKIES_FILE) or HERE
if os.path.abspath(_default_cookie_dir) == os.path.abspath(HERE):
    _default_cookie_dir = os.path.join(HERE, "cookies")
COOKIES_DIR = os.environ.get("COOKIES_DIR") or _default_cookie_dir
COOKIES_MERGED = os.path.join(tempfile.gettempdir(), "cobalt-dlp-cookies.txt")
COOKIES_LOCK = threading.Lock()
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
# 出口代理（可选）。整台服务器的 IP 被某站封了时，可以只让 yt-dlp 走代理：
#   YTDLP_PROXY    所有站点都走这个代理
#   YOUTUBE_PROXY  只让 YouTube 走这个代理（不设置则沿用 YTDLP_PROXY）
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "").strip()

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


def _looks_like_cookies(path):
    """粗筛：Netscape 格式有制表符，JSON 导出以 [ 或 { 开头。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"\t" in head or head.lstrip().startswith((b"[", b"{"))


def cookie_files():
    """All non-empty cookie files we should feed to yt-dlp."""
    found = []
    if os.path.isdir(COOKIES_DIR):
        for name in sorted(os.listdir(COOKIES_DIR)):
            if not name.lower().endswith(".txt"):
                continue
            path = os.path.join(COOKIES_DIR, name)
            try:
                if (os.path.isfile(path) and os.path.getsize(path) > 0
                        and _looks_like_cookies(path)):
                    found.append(os.path.abspath(path))
            except OSError:
                pass
    # 兼容老部署：COOKIES_FILE 指到目录外面时也要带上
    try:
        if (os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0
                and os.path.abspath(COOKIES_FILE) not in found):
            found.insert(0, os.path.abspath(COOKIES_FILE))
    except OSError:
        pass
    return found


def merged_cookies_path():
    """Return a single Netscape cookies file for yt-dlp (None if unset).

    yt-dlp 只接受一个 cookies 文件，所以把目录里的多个导出文件合成一份：
    丢弃重复的注释头、去掉完全重复的行，保留 #HttpOnly_ 开头的行。
    每次任务开始时重新合并，替换 cookies 后直接生效。
    """
    files = cookie_files()
    if not files:
        return None
    if len(files) == 1:
        return files[0]

    # 按修改时间从旧到新合并：同一站点重复的 cookie 以最新导出的为准
    try:
        files.sort(key=lambda p: os.path.getmtime(p))
    except OSError:
        pass

    lines, seen, header = [], set(), False
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#") and not s.startswith("#HttpOnly_"):
                if not header:
                    lines.append("# Netscape HTTP Cookie File")
                    header = True
                continue
            if s in seen:
                continue
            seen.add(s)
            lines.append(line)
    if not lines:
        return None

    with COOKIES_LOCK:
        fd, tmp = tempfile.mkstemp(prefix="cobalt-cookies-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, COOKIES_MERGED)   # 原子替换，别的任务不会读到半个文件
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return COOKIES_MERGED


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


# 国内 App 分享出来的是整段文字，链接夹在中文和表情符号中间，例如：
#   "7.20 复制打开抖音，看看【xxx的作品】… https://v.douyin.com/iAbCdEf/ 复制此链接…"
#   "97 xxx发布了一篇小红书笔记… http://xhslink.com/a/xxxx，复制本条信息…"
#   "【标题-哔哩哔哩】 https://b23.tv/xxxx"
# 字符集里排除：空白、引号、中英文标点、CJK 汉字/假名/全角符号。
# 分享链接后面常常直接跟着"复制此链接…"且没有空格，遇到 CJK 必须截断，
# 否则会把后面一大段中文当成 URL 的一部分。
URL_RE = re.compile(
    r"https?://[^\s<>\"'，。、；！？【】（）()\[\]{}「」『』《》"
    r"　㐀-䶿一-鿿＀-￯]+")
URL_TRAIL = ".,;:!?)]}>'\"，。、；：！？）】」』》"


def extract_url(text):
    """Pull the first http(s) URL out of arbitrary pasted text."""
    text = (text or "").strip()
    m = URL_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(URL_TRAIL)


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


EXTRACTOR_NAMES = {
    "douyin": "抖音", "tiktok": "TikTok",
    "xiaohongshu": "小红书", "xhs": "小红书",
    "bilibili": "B站", "bilibilibangumi": "B站",
    "youtube": "YouTube", "instagram": "Instagram",
    "twitter": "X（推特）", "weibo": "微博",
    "facebook": "Facebook", "reddit": "Reddit",
}

# 已从支持列表移除的平台：yt-dlp 提取器对所有自带测试链接都失败，
# 或根本无法验证。这里提前拦下，直接给出说明，而不是让用户等一轮
# yt-dlp 跑完再看到一句看不懂的报错。
REMOVED_PLATFORMS = (
    (re.compile(r"(^|\.)youku\.com$"),
     "优酷已不再支持：yt-dlp 的优酷提取器会被站点拦截（SSL 握手失败），"
     "自带的测试链接全部解析不了，已从支持列表移除。"),
    (re.compile(r"(^|\.)iqiyi\.com$"),
     "爱奇艺已不再支持：yt-dlp 的提取器对自带的测试链接全部报“找不到视频”，"
     "已从支持列表移除。"),
    (re.compile(r"(^|\.)douyu\.com$"),
     "斗鱼已不再支持：yt-dlp 的提取器无法解析房间信息（Unable to extract room id），"
     "已从支持列表移除。"),
    (re.compile(r"(^|\.)zhihu\.com$"),
     "知乎已不再支持：yt-dlp 没有可用的测试链接，视频还需要登录/付费，"
     "无法稳定下载，已从支持列表移除。"),
)


def removed_platform_message(url):
    host = _host_of(url)
    for pattern, message in REMOVED_PLATFORMS:
        if pattern.search(host):
            return message
    return ""


def explain_failure(lines, returncode):
    """把 yt-dlp 的报错尾巴翻译成一句能照着做的中文。

    直接展示 "yt-dlp exited with 1" 对用户没有任何帮助；这里按错误特征
    归类，原始输出仍保存在 job["log"] 里供展开查看。
    """
    raw = ""
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            raw = line[len("ERROR:"):].strip()
            break
    if not raw:
        raw = next((l.strip() for l in reversed(lines) if l.strip()), "")

    extractor = ""
    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw)
    if m:
        extractor = m.group(1)
        rest = re.sub(r"^[^:]{1,40}:\s*", "", m.group(2), count=1)
        raw_short = rest or m.group(2)
    else:
        raw_short = raw
    low = raw.lower()
    site = EXTRACTOR_NAMES.get(extractor.lower(), extractor)
    who = site or "该站点"
    if site and not re.search(r"[一-鿿]", site):
        who = site + " "

    if "fresh cookies" in low or "not necessarily logged in" in low:
        return (f"{who}要求 cookies（未登录状态的也行）。"
                "把从浏览器导出的 cookies 放进服务器 cookies/ 目录后重新下载即可，无需重启。")
    if ("sign in to confirm" in low or "not a bot" in low
            or ("confirm" in low and "bot" in low)):
        return ("YouTube 把服务器 IP 当成机器人了（已自动强制 IPv4 并轮换过 "
                "visionOS/android_vr/tv_downgraded/web_embedded 等客户端）。"
                "这个校验最终看 IP 信誉：请在浏览器登录 YouTube（建议用专门的小号）导出 cookies "
                "放进 cookies/ 目录，或给服务器换一个不被标记的出口 IP。")
    if "429" in low or "too many requests" in low or "rate limit" in low or "rate-limit" in low:
        return f"{who}限流了：等几分钟再试。配置该站 cookies 可以降低触发限流的概率。"
    if ("sign in" in low or "login" in low or "log in" in low
            or "cookies" in low or "authenticat" in low):
        return (f"{who}需要登录信息：请在 cookies/ 目录里配置该站 cookies 后重试。")
    if "private" in low:
        return "内容是私密的，需要作者授权或配置 cookies。"
    if "unsupported url" in low:
        return "yt-dlp 不支持这个链接，换一个视频页面的链接试试。"
    if "requested content is not available" in low or "media not found" in low:
        return "在" + who.rstrip() + "上找不到这段内容：可能是私密/已删除，或需要配置 cookies 登录后再试。"
    if ("unavailable" in low or "not available" in low or "removed" in low
            or "not exist" in low or "not found" in low):
        return "视频不存在，或已被删除 / 设为不可见。"
    if "412" in low or "precondition failed" in low:
        return ("目标站点风控拦截（HTTP 412）：B 站对机房/海外 IP 限制很严。"
                "程序已自动补 buvid3/buvid4/b_nut 并换浏览器 TLS 指纹重试；"
                "仍失败时最有效的办法是在浏览器登录 B 站后导出 cookies（含 SESSDATA、buvid3）"
                "放进 cookies/ 目录，或者给服务器配一个国内出口代理。")
    if "403" in low:
        return f"{who}拒绝了请求（HTTP 403），通常需要 cookies 或更换服务器出口网络。"
    if "404" in low:
        return "链接失效（HTTP 404）。"
    if "no video formats found" in low:
        return ("没有找到可下载的视频流，也没能提取到页面里的图片："
                "可能是图文/纯图片内容，或该站提取器暂时失效（可更新 yt-dlp）。")
    if "requested format is not available" in low or "format is not available" in low:
        return ("没有可用的下载格式：YouTube 在机房 IP 上常只返回受限流（SABR）。"
                "程序已自动强制 IPv4 并轮换客户端；仍失败请确认 cookies/ 里的 YouTube cookies "
                "确实是登录态（含 SID/LOGIN_INFO），或更换不被标记的出口 IP。")
    if "can't find any video" in low or "cannot find any video" in low:
        return ("页面里没找到视频数据：可能是会员/付费内容、地区限制，"
                "或 yt-dlp 的提取器失效（更新 yt-dlp 后再试）。")
    if "ssr_hydrated_data" in low or "ssr data" in low:
        return "站点页面结构变了，yt-dlp 暂时解析不了这个站；更新 yt-dlp 后再试。"
    if "unable to extract room id" in low:
        return "直播间解析失败：链接可能已过期，或该站改了接口。"
    if "unexpected_eof" in low or "ssl:" in low or "ssl_connect" in low:
        return ("和站点建立连接被拒绝（机房 IP 常拦 Python 的 TLS 指纹）。"
                "已自动尝试浏览器 TLS 指纹；仍失败请换出口网络或配置该站 cookies。")
    if "unable to extract" in low:
        return "解析失败：站点可能改版，先更新 yt-dlp（docker compose exec savebox pip install -U yt-dlp）再试。"
    if "country" in low or "region" in low or "geo-restrict" in low:
        return "该内容有地区限制，服务器所在地区无法访问。"
    if ("connect tunnel failed" in low or "proxyerror" in low
            or "unable to connect to proxy" in low or "proxy connect" in low):
        return ("代理连接失败：请确认 YOUTUBE_PROXY / YTDLP_PROXY 填的是代理地址"
                "（形如 http://用户名:密码@主机:端口 或 socks5://主机:端口），"
                "而不是服务商的 API / 订阅链接。若代理用 IP 白名单，先加白服务器出口 IP。")
    if ("timed out" in low or "timeout" in low or "name or service" in low
            or "connection" in low or "network" in low or "temporary failure" in low):
        return "网络连接失败或超时，稍后重试；持续失败可能是服务器网络问题。"
    if raw_short:
        return f"下载失败：{raw_short}"
    return f"下载失败（yt-dlp 退出码 {returncode}）。"


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
    rotation = 0
    if vid is not None:
        for sd in vid.get("side_data_list") or []:
            if "rotation" in sd:
                try:
                    rotation = int(float(sd["rotation"]))
                except (TypeError, ValueError):
                    pass
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
        "sar": (vid or {}).get("sample_aspect_ratio"),
        "rotation": rotation,
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
    sar = info.get("sar") or ""
    if sar not in ("", "1:1", "0:1", "N/A"):
        # 非方形像素：有些播放器按显示宽高比播放，有些忽略 SAR，
        # 同一个文件在不同手机上比例会不一样。统一转成 1:1。
        try:
            sa, sb = (int(x) for x in sar.split(":"))
            if sa != sb:
                return f"像素宽高比 {sar} 不是 1:1（手机上可能被拉变形）"
        except (ValueError, TypeError):
            pass
    rot = info.get("rotation") or 0
    if rot % 360 != 0:
        return f"视频带 {rot}° 旋转标记，部分播放器不认（会方向/比例错乱）"
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

    # 宽高取偶数（H.264 要求）；同时把非方形像素换算成方形像素（setsar=1），
    # 否则同一个文件在 iPhone 和 Chrome 上可能一个正常一个被拉变形。
    vf = "scale=w='trunc(iw*sar/2)*2':h=trunc(ih/2)*2,setsar=1"
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


THUMB_FILE = ".thumb.jpg"


def make_thumb(video_path, job_dir, job_id):
    """Grab one frame with ffmpeg and store it as a small jpg.

    The list uses <video preload="metadata"> as a thumbnail, but iOS/Safari
    (and some Chrome builds) won't decode any frame until the user presses
    play, so the tile shows up as a black square. A real image fixes that
    everywhere. Returns "<jobid>/.thumb.jpg" or None if ffmpeg can't.
    """
    out = os.path.join(job_dir, THUMB_FILE)
    # 先抽 1 秒处，避开很多视频开头的黑场/淡入；短视频不够 1 秒就退回第 0 帧
    for seek in ("1", "0"):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-ss", seek, "-i", video_path,
               "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4",
               "-f", "image2", out]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
        except FileNotFoundError:
            return None
        except Exception:  # noqa
            continue
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return job_id + "/" + THUMB_FILE
        try:
            os.remove(out)
        except OSError:
            pass
    return None


# ---------------------------------------------------------------- 图片 / 图文
# yt-dlp 只为"视频流"设计：图文帖子（抖音/小红书/Instagram 图片帖）没有
# formats，会直接报 "No video formats found"。这里补一层图片通道：
#   1) 链接本身就是图片 → 直接下载（不走 yt-dlp）
#   2) 抖音图文 → 内置下载器从 aweme_detail.images 取原图
#   3) 其他平台 → 让 yt-dlp 用 --ignore-no-formats-error 输出 JSON，
#      从 thumbnails 里取出页面上的图片并逐张下载
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|avif)$", re.I)
# 出现这些错误说明"页面里根本没有视频"，才值得再试图片；
# YouTube 机器人校验之类的视频失败绝不能拿封面图冒充下载结果。
IMAGE_FALLBACK_ERRORS = (
    "no video formats found", "there is no video in this post",
    "no video could be found in this tweet", "no video in this post",
    "unsupported url", "no formats found",
)
IMAGE_REFERERS = (
    ("douyin.com", "https://www.douyin.com/"),
    ("douyinpic.com", "https://www.douyin.com/"),
    ("xiaohongshu.com", "https://www.xiaohongshu.com/"),
    ("xhscdn.com", "https://www.xiaohongshu.com/"),
    ("instagram.com", "https://www.instagram.com/"),
    ("cdninstagram.com", "https://www.instagram.com/"),
    ("fbcdn.net", "https://www.instagram.com/"),
    ("twimg.com", "https://x.com/"),
    ("pinterest.com", "https://www.pinterest.com/"),
    ("pinimg.com", "https://www.pinterest.com/"),
    ("weibo.com", "https://weibo.com/"),
    ("sinaimg.cn", "https://weibo.com/"),
    ("bilibili.com", "https://www.bilibili.com/"),
    ("hdslb.com", "https://www.bilibili.com/"),
)


class ImageError(Exception):
    """图片下载失败（已经是给用户看的中文）。"""


def is_image_name(name):
    return (name or "").lower().endswith(IMAGE_EXTS)


def make_image_thumb(image_path, job_dir, job_id):
    """Small jpg for the task list; using the full photo as an icon is too heavy."""
    if not FFMPEG:
        return None
    out = os.path.join(job_dir, THUMB_FILE)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", image_path, "-frames:v", "1",
           "-vf", "scale='min(320,iw)':-2", "-q:v", "4", "-f", "image2", out]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:  # noqa
        return None
    if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return job_id + "/" + THUMB_FILE
    try:
        os.remove(out)
    except OSError:
        pass
    return None


def looks_like_image_url(url):
    p = urlparse(url or "")
    if IMAGE_URL_RE.search(p.path or ""):
        return True
    q = parse_qs(p.query or "")
    for key in ("format", "fm"):
        ext = (q.get(key) or [""])[0].lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif", "avif", "bmp"):
            return True
    return False


def image_referer(url):
    """图片 CDN 常检查 Referer：按图片域名给对应站点的首页，避免 403。"""
    host = (urlparse(url or "").hostname or "").lower()
    for suffix, referer in IMAGE_REFERERS:
        if host == suffix or host.endswith("." + suffix):
            return referer
    p = urlparse(url or "")
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}/"
    return ""


def image_title(url):
    """从图片直链里取一个适合做文件名的标题。"""
    stem = os.path.splitext(os.path.basename(unquote(urlparse(url or "").path)))[0]
    return image_stem(stem or "image")


def image_stem(title):
    """文件名前缀：保留中文，去掉路径/非法字符，且不能以点开头（否则会被隐藏）。"""
    stem = safe_name(re.sub(r"[\r\n\t]+", " ", title or ""))
    return stem.lstrip("._") or "image"


def cookie_header_for(url, cookies_path):
    """按 URL 的主机从合并后的 Netscape cookies 里拼出 Cookie 头。"""
    host = (urlparse(url or "").hostname or "").lower()
    if not host or not cookies_path or not os.path.isfile(cookies_path):
        return ""
    pairs = []
    try:
        with open(cookies_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    req_path = urlparse(url or "").path or "/"
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        elif s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) < 7:
            continue
        domain, cpath, name, value = parts[0].lstrip(".").lower(), parts[2] or "/", parts[5], parts[6]
        if not (host == domain or host.endswith("." + domain)):
            continue
        if not req_path.startswith(cpath):
            continue
        try:
            expires = int(parts[4] or 0)
        except ValueError:
            expires = 0
        if expires and expires < time.time():
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(dict.fromkeys(pairs))


def _sniff_image_ext(head):
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:2] == b"BM":
        return ".bmp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


def _mime_image_ext(ctype):
    return {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
        "image/avif": ".avif",
    }.get((ctype or "").lower().split(";")[0].strip())


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{base}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def download_image(url, dest_stem, job=None, referer=None, cookies_path=None):
    """Download one image. Returns the saved path, or None if the URL isn't an image.

    返回 None 表示"内容不是图片"（比如热链拦截返回了 HTML），调用方可以改走
    其它策略；网络/HTTP 错误则抛 ImageError，避免把真实失败悄悄吞掉。
    """
    headers = {
        "User-Agent": UA_DESKTOP,
        # 不主动要 AVIF：老版本 iOS 相册存不了，宁可让服务器给 JPEG/WebP
        "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
    }
    ref = referer or image_referer(url)
    if ref:
        headers["Referer"] = ref
    ck = cookie_header_for(url, cookies_path)
    if ck:
        headers["Cookie"] = ck

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            head = r.read(65536)
            if not head:
                raise ImageError("图片地址返回了空内容")
            ext = _sniff_image_ext(head) or _mime_image_ext(r.headers.get("Content-Type"))
            if not ext:
                return None
            dest = _unique_path(dest_stem + ext)
            tmp = dest + ".part"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(head)
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                    raise ImageError("图片内容为空")
                os.replace(tmp, dest)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            return dest
    except urllib.error.HTTPError as e:
        raise ImageError(f"图片服务器返回 HTTP {e.code}（可能需要 cookies 或更换出口网络）")
    except ImageError:
        raise
    except Exception as e:  # noqa
        raise ImageError(f"图片下载失败：{e}")


def direct_image_download(job, job_dir, url, cookies_path):
    """Download a direct image link. Returns a path, or None if it's not an image."""
    with JOBS_LOCK:
        job["phase"] = "downloading"
        job["progress"] = 0.0
    dest = download_image(url, os.path.join(job_dir, image_title(url)), job,
                          referer=image_referer(url), cookies_path=cookies_path)
    if dest:
        with JOBS_LOCK:
            job["title"] = os.path.splitext(os.path.basename(dest))[0]
            job["progress"] = 100.0
    return dest


def yt_dlp_dump_json(job, cookies_path):
    """Ask yt-dlp for the info dict without downloading anything."""
    cmd = YTDLP + [
        "--dump-single-json", "--skip-download", "--ignore-no-formats-error",
        "--no-warnings", "--no-color", "--socket-timeout", "20",
        "--retries", "3", "--extractor-retries", "2",
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd += _proxy_args(_host_of(job["url"])) + [job["url"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except Exception:  # noqa
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        m = re.search(r"(\{.*\})\s*$", out, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None


def collect_image_candidates(info):
    """Pick one best image URL per picture out of a yt-dlp info dict.

    Extractor 的 thumbnails 里同一张图常有多档尺寸：去掉 query / `!` 处理参数后
    地址相同的归为一组，取像素面积最大的一张。纯视频条目跳过（它们的封面不是
    用户要的图片）。
    """
    found = []  # (url, width, height, title)

    def walk(node):
        if not isinstance(node, dict):
            return
        entries = node.get("entries")
        if entries:
            for entry in entries:
                walk(entry)
            return
        if node.get("formats"):
            return
        title = node.get("title") or node.get("description") or ""
        for thumb in node.get("thumbnails") or []:
            if not isinstance(thumb, dict):
                continue
            u = thumb.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                found.append((u, int(thumb.get("width") or 0),
                              int(thumb.get("height") or 0), title))
        u = node.get("url")
        if isinstance(u, str) and u.startswith(("http://", "https://")) and looks_like_image_url(u):
            found.append((u, 0, 0, title))

    walk(info)

    groups, order = {}, []
    for u, w, h, title in found:
        key = re.split(r"[!?#]", u, 1)[0]   # 去掉缩放参数，得到图片本体地址
        # Pinterest 把尺寸写在路径里（/736x/、/originals/），归一到同一个地址
        key = re.sub(r"/(?:\d+x\d*|originals)/", "/", key)
        idx = groups.get(key)
        if idx is None:
            groups[key] = len(order)
            order.append((u, w, h, title))
        else:
            cur = order[idx]
            if w * h > cur[1] * cur[2]:
                order[idx] = (u, w, h, title)

    out = []
    for u, w, h, title in order:
        if w and h and w < 300 and h < 300:   # 头像/图标之类的小图
            continue
        out.append((u, w, h, title))
    return out


def save_images(job, job_dir, candidates, page_url, cookies_path):
    """Download all candidate images into the job directory."""
    n = len(candidates)
    title = next((t for _, _, _, t in candidates if t), "") or image_title(page_url)
    title = image_stem(title)
    saved = []
    with JOBS_LOCK:
        job["phase"] = "downloading"
        job["progress"] = 0.0
    for i, (url, _w, _h, _t) in enumerate(candidates, 1):
        stem = title + (f"_{i:02d}" if n > 1 else "")
        try:
            dest = download_image(url, os.path.join(job_dir, stem), job,
                                  referer=image_referer(url), cookies_path=cookies_path)
        except ImageError as e:
            _job_note(job, f"第 {i} 张图片下载失败：{e}")
            dest = None
        if dest:
            saved.append(dest)
        else:
            _job_note(job, f"第 {i} 张不是图片，已跳过")
        with JOBS_LOCK:
            job["progress"] = round(i * 100.0 / n, 1)
    if not saved:
        raise ImageError("没有下载到任何图片（链接可能已失效，或该内容需要 cookies 登录）")
    with JOBS_LOCK:
        job["title"] = title
    return saved


def download_images_fallback(job, job_dir, cookies_path):
    """No video was found: try to save the images on the page instead."""
    info = yt_dlp_dump_json(job, cookies_path)
    candidates = collect_image_candidates(info) if info else []
    if candidates:
        return save_images(job, job_dir, candidates, job["url"], cookies_path)
    # 有些图片直链没有扩展名，yt-dlp 会报 Unsupported URL；直接按图片再试一次
    try:
        dest = direct_image_download(job, job_dir, job["url"], cookies_path)
    except ImageError:
        dest = None
    return [dest] if dest else []


def make_image_archive(job_dir, job_id, files, title):
    """Bundle multiple images into one zip so phone users can save them at once."""
    stem = image_stem(title or "images")
    path = _unique_path(os.path.join(job_dir, stem + ".zip"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for f in files:
            zf.write(os.path.join(job_dir, f["name"]), arcname=f["name"])
    return job_id + "/" + os.path.basename(path)


def _job_failed(job, message, log_lines=None):
    with JOBS_LOCK:
        job["status"] = "error"
        job["error"] = message
        job["log"] = "\n".join(log_lines or [])
        job["finished"] = now_ms()
        job["updated"] = now_ms()


# ---------------------------------------------------------------- 抖音
# yt-dlp 的 Douyin 提取器只请求 aweme/detail 而且不带签名，抖音的 Argus 网关
# 一律 403（"Uifid Not Found" / "Signature Not Found"），换任何 cookies 都不行。
# 这里自己按 x-secsdk-web-signature 的算法签名（uifid + timestamp + 固定盐值），
# 请求头再带上 uifid，才能拿到 aweme_detail；cookies 里必须有 UIFID。
DOUYIN_HOST_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:douyin|iesdouyin)\.com/", re.I)
# 视频是 /video/<id>，图文作品是 /note/<id>；短链 v.douyin.com 会重定向到其中之一
DOUYIN_ID_RE = re.compile(r"/(?:video|note|share/video|share/note|share/slides)/(\d{10,25})")
DOUYIN_SECSALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 手机分享页（www.iesdouyin.com）用的 UA；图文接口的 slidesinfo/iteminfo 是
# 分享页自己调的，带上手机 UA 更接近真实请求。
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")


class DouyinError(Exception):
    """抖音专用下载路径的失败原因（已经是给用户看的中文）。"""


def douyin_video_id(url):
    """从视频/图文页或 v.douyin.com 短链里取出 aweme_id（取不到返回 ""）。"""
    m = DOUYIN_ID_RE.search(url or "")
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url or "").query)
    for key in ("modal_id", "aweme_id", "item_ids"):
        value = (q.get(key) or [""])[0]
        if re.fullmatch(r"\d{10,25}", value or ""):
            return value
    m = re.match(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+", url or "", re.I)
    if not m:
        return ""
    try:
        req = urllib.request.Request(m.group(0), headers={"User-Agent": UA_DESKTOP})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
    except Exception:
        return ""
    m = DOUYIN_ID_RE.search(final)
    return m.group(1) if m else ""


def _parse_douyin_cookies(path):
    """(Cookie 头, 签名用的 uifid, cookie 数量, 是否找到 uifid)。

    Douyin 会同时下发 UIFID 和 UIFID_TEMP，两者都能过 Argus 校验，优先 UIFID。
    """
    if not path or not os.path.isfile(path):
        return "", "", 0, False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return "", "", 0, False

    pairs, uifids = [], {}

    def add(domain, name, value):
        if "douyin.com" not in (domain or "").lower() or not name:
            return
        pairs.append(f"{name}={value}")
        if name.upper().startswith("UIFID"):
            uifids[name.upper()] = value   # 后出现的覆盖先出现的（合并顺序=旧→新）

    # 标准 Netscape 格式（浏览器扩展的默认导出）
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        elif s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) >= 7:
            add(parts[0], parts[5].strip(), parts[6].strip())

    # JSON 导出（EditThisCookie / Cookie-Editor）
    if not pairs and text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else (data.get("cookies") or [])
            for item in items:
                if isinstance(item, dict):
                    host = item.get("domain") or urlparse(item.get("url") or "").hostname or ""
                    add(host, str(item.get("name") or ""), str(item.get("value") or ""))
        except ValueError:
            pass

    # 兜底：有些工具把 tab 换成了空格，按空白切 7 段
    if not pairs and not text.lstrip().startswith(("[", "{")):
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#HttpOnly_"):
                s = s[len("#HttpOnly_"):]
            elif s.startswith("#"):
                continue
            f = s.split(maxsplit=6)
            if len(f) == 7:
                add(f[0], f[5], f[6])

    # 还不行就按行抓 UIFID
    if not uifids:
        for m in re.finditer(r"(?m)^.*douyin\.com.*?\b(UIFID(?:_TEMP)?)\s+(\S+)\s*$", text):
            add(".douyin.com", m.group(1), m.group(2))
            break

    uifid = uifids.get("UIFID") or uifids.get("UIFID_TEMP") or next(iter(uifids.values()), "")
    return "; ".join(dict.fromkeys(pairs)), uifid, len(pairs), bool(uifid)


def _douyin_cookie_names(path):
    """Cookie 名字列表（不含值），只用于 /api/health 排查上传是否正确。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    names = set()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        elif s.startswith("#"):
            continue
        f = s.split("\t")
        if len(f) >= 7 and "douyin.com" in f[0].lower() and f[5].strip():
            names.add(f[5].strip())
    return sorted(names)[:12]


def douyin_sign(url, uifid, ts=None):
    """规范化 query 并算出 x-secsdk-web-signature（纯计算，无需 JS）。"""
    ts = ts or int(time.time())
    base, _, query = url.partition("?")
    safe = "!*'()"
    parts = []
    for pair in query.split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        k = unquote_plus(k)
        v = unquote_plus(v)
        parts.append(f"{k}={quote(v, safe=safe)}")
    canon = "&".join(parts)
    names = [p.split("=", 1)[0] for p in canon.split("&") if p]
    if "uifid" not in names:
        canon = (canon + "&" if canon else "") + "uifid=" + quote(uifid, safe=safe)
    signed = f"{canon}&timestamp={ts}"
    plain = f"{uifid}_{ts}_{DOUYIN_SECSALT}_{signed}"
    sig = hashlib.md5(plain.encode("utf-8")).hexdigest()
    return f"{base}?{signed}&x-secsdk-web-signature={sig}"


def douyin_abogus_params(params, user_agent=UA_MOBILE):
    """在请求参数里补上 a_bogus（抖音 web 接口的加密校验参数）。

    模块缺失或算法异常时返回 None，调用方跳过这个兜底而不是让整个任务失败。
    """
    try:
        from douyin_abogus import ABogus
    except Exception:  # noqa
        return None
    signed = dict(params)
    try:
        signed["a_bogus"] = ABogus(user_agent).generate_a_bogus(signed)
    except Exception:  # noqa
        return None
    return signed


def _douyin_get_json(url, headers, timeout=25):
    """GET 一个 JSON 接口，返回 (data, 错误说明)。"""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read(300).decode("utf-8", "replace").strip()
        except Exception:  # noqa
            text = ""
        return None, f"HTTP {e.code}" + (f" {text[:80]}" if text else "")
    except Exception as e:  # noqa
        return None, str(e)[:100]
    try:
        return json.loads(body or b"{}"), ""
    except ValueError:
        return None, "返回的不是 JSON"


def _douyin_pick_detail(data, video_id):
    """从 detail / iteminfo / slidesinfo 的响应里取出这个作品本身。"""
    if not isinstance(data, dict):
        return None
    detail = data.get("aweme_detail")
    if isinstance(detail, dict) and detail:
        return detail
    for key in ("aweme_details", "aweme_list"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        items = [item for item in items if isinstance(item, dict) and item]
        for item in items:
            if str(item.get("aweme_id") or "") == str(video_id):
                return item
        # 有些接口会省略 aweme_id；只有确实没写 ID 时才接受列表里的第一条
        for item in items:
            if not item.get("aweme_id"):
                return item
    return None


def douyin_fetch_note_detail(video_id, uifid, cookie_header):
    """图文作品（/note/）最后兜底：按手机分享页的请求试 iteminfo / slidesinfo。

    主接口（aweme/detail?aid=6383）正常情况下已经能拿到图文作品；这里只在
    它仍然为空时再试。分享页拉图文用的就是这两个接口，query 里要带 a_bogus。
    返回 (detail, 诊断信息列表)。
    """
    web_id = "77" + "".join(random.choice("0123456789") for _ in range(17))
    common = {"reflow_source": "reflow_page", "web_id": web_id,
              "device_id": web_id}
    headers = {
        "User-Agent": UA_MOBILE,
        "Referer": f"https://www.iesdouyin.com/share/note/{video_id}/",
        "uifid": uifid,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_header,
    }
    attempts = [
        ("iteminfo", "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/", [
            dict(common, item_ids=f'["{video_id}"]', aweme_type="68",
                 aid="6383"),
            dict(common, item_ids=f'["{video_id}"]'),
        ]),
        ("slidesinfo", "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/", [
            dict(common, aweme_ids=f"[{video_id}]", aweme_type="68",
                 aid="6383", request_source="200"),
            dict(common, aweme_ids=f"[{video_id}]"),
        ]),
    ]
    reasons = []
    for name, base_url, variants in attempts:
        for params in variants:
            signed = douyin_abogus_params(params)
            if signed is None:
                reasons.append(f"{name}: a_bogus 不可用")
                break
            data, err = _douyin_get_json(base_url + "?" + urlencode(signed),
                                         headers, timeout=12)
            detail = _douyin_pick_detail(data, video_id)
            if detail:
                return detail, reasons
            if data:
                msg = f"status_code={data.get('status_code')}"
                if data.get("status_msg"):
                    msg += f" {data.get('status_msg')}"
                if data.get("filter_list"):
                    msg += f" filter={data.get('filter_list')}"
                reasons.append(f"{name}: {msg}")
            else:
                reasons.append(f"{name}: {err}")
    return None, reasons


def douyin_fetch_detail(video_id, cookies_path):
    cookie_header, uifid, count, _ = _parse_douyin_cookies(cookies_path)
    if not uifid:
        files = len(cookie_files())
        raise DouyinError(
            f"抖音 cookies 里没有 UIFID（合并后的文件里有 {count} 个抖音 cookie，"
            f"共 {files} 个 cookies 文件）。请在浏览器登录 douyin.com 后重新导出 cookies，"
            f"确认文件里有一行以 UIFID 开头的记录，放进 cookies/ 目录再试。")
    # aid=6383 是网页端 app id：不带它时抖音会把图文作品判成 images_base
    # 并返回 aweme_detail=null（视频不受影响，所以原来只在图文上暴露出来）。
    url = douyin_sign(
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/"
        f"?aweme_id={video_id}&aid=6383", uifid)
    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.douyin.com/",
        "uifid": uifid,
        "Accept": "application/json",
        "Cookie": cookie_header,
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(200).decode("utf-8", "replace")
        except Exception:  # noqa
            detail = ""
        if "Uifid" in detail:
            raise DouyinError(
                "抖音风控拦截（Uifid Not Found）：cookies 里没有有效的 UIFID，"
                "重新导出 cookies 再试。")
        if "Signature" in detail:
            raise DouyinError(
                "抖音风控拦截（Signature Not Found）：cookies 可能已过期，"
                "重新导出 cookies 再试。")
        raise DouyinError(f"抖音接口返回 HTTP {e.code}：{detail[:120] or '无内容'}")
    except Exception as e:  # noqa
        raise DouyinError(f"请求抖音接口失败：{e}")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise DouyinError("抖音接口返回的不是 JSON，可能被风控了，稍后重试。")
    detail = data.get("aweme_detail")
    if not detail:
        # 图文作品走 detail 有时只回 aweme_detail=null，改用分享页的
        # iteminfo / slidesinfo 兜底（这两条接口的返回字段仍是 images/video）。
        fallback, reasons = douyin_fetch_note_detail(
            video_id, uifid, cookie_header)
        if fallback:
            return fallback
        why = (data.get("filter_detail") or {}).get("filter_reason") or ""
        extra = "；".join(([why] if why else []) + reasons[:3])
        raise DouyinError(
            "抖音没有返回这个作品：可能已删除 / 私密，或 cookies 过期。"
            + (f"（{extra}）" if extra else ""))
    return detail


def douyin_pick_url(detail):
    """优先 H.264（iPhone 直接能放，省一次转码）。"""
    video = detail.get("video") or {}
    for key in ("play_addr_h264", "play_addr"):
        urls = (video.get(key) or {}).get("url_list") or []
        if urls:
            return urls[0]
    best, best_br = "", -1
    for item in video.get("bit_rate") or []:
        urls = (item.get("play_addr") or {}).get("url_list") or []
        br = item.get("bit_rate") or 0
        if urls and br > best_br:
            best, best_br = urls[0], br
    return best


def douyin_title(detail):
    desc = (detail.get("desc") or "").strip() or str(detail.get("aweme_id") or "douyin")
    desc = re.sub(r"[\r\n\t]+", " ", desc)
    desc = re.sub(r'[\\/:*?"<>|]+', " ", desc)
    desc = re.sub(r"\s{2,}", " ", desc).strip(" .")[:80].strip(" .")
    return desc or "douyin"


def douyin_filename(detail):
    return douyin_title(detail) + ".mp4"


def douyin_image_urls(detail):
    """图文作品的原图地址（每组取一张，优先无水印的 url_list）。

    download_url_list 走的是 ~tplv-dy-water-* 模板，右下角带「抖音号：xxx」
    水印；url_list 是网页展示用的 ~tplv-dy-aweme-images-* 模板，不带水印。
    两者分辨率相同，所以优先 url_list，只有它缺失时才退回 download_url_list。
    """
    items = detail.get("images")
    if not isinstance(items, list) or not items:
        items = (detail.get("image_post_info") or {}).get("images") or []
    flat = []
    for item in items:
        if isinstance(item, list):      # 个别版本会多包一层数组
            flat.extend(item)
        else:
            flat.append(item)
    urls = []
    for item in flat:
        if isinstance(item, str):
            urls.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("url_list", "download_url_list"):
            candidates = item.get(key) or []
            url = next((u for u in candidates
                        if isinstance(u, str) and u.startswith(("http://", "https://"))), "")
            if url:
                urls.append(url)
                break
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def _http_to_file(url, dest, job, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total and job is not None:
                with JOBS_LOCK:
                    job["progress"] = min(99.0, round(done * 100.0 / total, 1))


def douyin_to_mp3(video_path, job):
    out = os.path.splitext(video_path)[0] + ".mp3"
    if not FFMPEG:
        raise DouyinError("未安装 ffmpeg，无法提取音频。")
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", video_path, "-vn", "-c:a", "libmp3lame", "-q:a", "0", out],
            capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise DouyinError("提取音频超时。")
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        err = (r.stderr or "").strip().splitlines()
        raise DouyinError("提取音频失败：" + (err[-1] if err else f"ffmpeg exit {r.returncode}"))
    try:
        os.remove(video_path)
    except OSError:
        pass
    return out


def _bilibili_visitor_cookies():
    """取一套 B 站访客 cookie：首页给 buvid3 + b_nut，SPI 再补 buvid4。

    只带 buvid3 时，机房 IP 仍可能 412；buvid3 和 b_nut 是同一次访问下发的，
    要成对使用。首页拿不到再退回 SPI 的 b_3/b_4。
    """
    cookies = {}
    # 先访问首页：这里下发的一组 buvid3/b_nut 才是"浏览器一致"的
    try:
        for c in _fetch_visitor_cookies("https://www.bilibili.com/"):
            if c.name in ("buvid3", "buvid4", "b_nut") and c.value:
                cookies[c.name] = c.value
    except Exception:  # noqa
        pass
    try:
        req = urllib.request.Request("https://api.bilibili.com/x/frontend/finger/spi",
                                     headers={"User-Agent": UA_DESKTOP})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read() or b"{}")
        d = data.get("data") or {}
        if d.get("b_3"):
            cookies.setdefault("buvid3", d["b_3"])
        if d.get("b_4"):
            cookies.setdefault("buvid4", d["b_4"])
    except Exception:  # noqa
        pass
    return cookies


def cookies_with_buvid(cookies_path, job_dir, job=None):
    """B 站从机房 IP 访问常返回 412，需要 buvid3/buvid4 访客 cookie。

    用户没配 B 站 cookies 时，自动要一套访客 cookie 补进 cookies 文件；
    已经有登录 cookies 的用户也补上 b_nut，少踩一次风控。
    """
    text = ""
    if cookies_path:
        try:
            with open(cookies_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
    has_buvid3 = bool(re.search(r"\tbuvid3\t", text))
    has_nut = bool(re.search(r"\tb_nut\t", text))
    if has_buvid3 and has_nut:
        return cookies_path
    fresh = _bilibili_visitor_cookies()
    if has_nut:
        fresh.pop("b_nut", None)
    # 只有 buvid3、没有 b_nut 时，那份 buvid3 多半是过期的（或者格式不对），
    # 这里用新取的一套覆盖掉。
    if not fresh:
        return cookies_path
    out = os.path.join(job_dir, "bili-cookies.temp")
    exp = int(time.time()) + 365 * 86400
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        if text:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        fh.write("# Netscape HTTP Cookie File\n")
        for name, value in fresh.items():
            fh.write(f".bilibili.com\tTRUE\t/\tFALSE\t{exp}\t{name}\t{value}\n")
    _job_note(job, "B站：已自动补充访客 cookie（buvid3/buvid4/b_nut），用于绕过 412 风控")
    return out


# 有些站点要"访客 cookie"才肯放行（小红书 a1、微博 SUB）。第一次访问首页就会下发，
# 用户没配 cookies 时自动去要一份，不用手动导出。
VISITOR_SEEDS = {
    "xiaohongshu.com": ("https://www.xiaohongshu.com/", "a1"),
    "weibo.com": ("https://weibo.com/", "SUB"),
    "weibo.cn": ("https://m.weibo.cn/", "SUB"),
}


def _fetch_visitor_cookies(seed_url):
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(seed_url, headers={
        "User-Agent": UA_DESKTOP,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with opener.open(req, timeout=15) as r:
        r.read(4096)
    return [c for c in jar if c.name]


def cookies_with_visitor(host, cookies_path, job_dir, job=None):
    match = next(((s, seed, key) for s, (seed, key) in VISITOR_SEEDS.items()
                  if host.endswith(s)), None)
    if not match:
        return cookies_path
    site, seed_url, key = match
    text = ""
    if cookies_path:
        try:
            with open(cookies_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
    if key and re.search(rf"\t{re.escape(key)}\t", text):
        return cookies_path
    try:
        items = _fetch_visitor_cookies(seed_url)
    except Exception:  # noqa
        return cookies_path
    existing = set()
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) >= 7:
            existing.add((f[0].lstrip("."), f[5]))
    fresh = [c for c in items
             if (c.domain or "").lstrip(".").endswith(site)
             and ((c.domain or "").lstrip("."), c.name) not in existing]
    if not fresh:
        return cookies_path
    out = os.path.join(job_dir, "visitor-cookies.temp")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        if text:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        fh.write("# Netscape HTTP Cookie File\n")
        for c in fresh:
            value = str(c.value or "").replace("\t", "").replace("\n", "").replace("\r", "")
            fh.write(f"{c.domain or '.' + site}\tTRUE\t/\tFALSE\t{int(c.expires or 0)}\t"
                     f"{c.name}\t{value}\n")
    _job_note(job, f"{site}：已自动补充访客 cookie（{len(fresh)} 个）")
    return out


def _host_of(url):
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:  # noqa
        return ""


def _proxy_args(host):
    is_youtube = host.endswith(("youtube.com", "youtu.be", "youtube-nocookie.com"))
    proxy = (YOUTUBE_PROXY or YTDLP_PROXY) if is_youtube else YTDLP_PROXY
    return ["--proxy", proxy] if proxy else []


def yt_dlp_attempts(job):
    """按顺序尝试的 (说明, -f 选择器, 额外参数)。"""
    fmt = QUALITY_FMT.get(job.get("quality", "best"), QUALITY_FMT["best"])
    host = _host_of(job["url"])
    out = [("默认", fmt, [])]
    if host.endswith(("youtube.com", "youtu.be", "youtube-nocookie.com")):
        # 先强制 IPv4：GCP/部分 VPS 的 IPv6 段被 YouTube 整段降权，IPv4 往往就能过。
        out[0] = ("默认（IPv4）", fmt, ["--force-ipv4"])
        # 再轮换客户端：visionos/android_vr/tv_downgraded/web_embedded 这几家
        # 不强制要 PO token，实测最抗机房 IP 的风控。
        for label, client in (
            ("visionOS 客户端", "visionos"),
            ("android_vr 客户端", "android_vr"),
            ("tv_downgraded 客户端", "tv_downgraded"),
            ("web_embedded 客户端", "web_embedded"),
            ("mweb 客户端", "mweb"),
            ("tv 客户端", "tv"),
            ("safari 客户端", "web_safari"),
        ):
            out.append((f"YouTube {label}", fmt,
                        ["--force-ipv4",
                         "--extractor-args", f"youtube:player_client={client}"]))
    elif host.endswith(("x.com", "twitter.com")):
        # 机房 IP 上 GraphQL 接口可能被限流；syndication 接口不需要登录，
        # 在拿不到 guest token 时通常还能取到视频地址。
        out.append(("X syndication 接口", fmt,
                    ["--extractor-args", "twitter:api=syndication"]))
    if job.get("quality") != "best":
        out.append(("最佳可用画质", QUALITY_FMT["best"], []))
    # 部分站点会拒绝 Python 的 TLS 指纹；装了 curl_cffi
    # 就用浏览器的 TLS 指纹再试一次。
    try:
        import curl_cffi  # noqa: F401
        out.append(("浏览器 TLS 指纹", fmt, ["--impersonate", "chrome"]))
    except Exception:  # noqa
        pass
    proxy = _proxy_args(host)
    if proxy:
        out = [(label, f, extra + proxy) for label, f, extra in out]
    return out


_RETRYABLE = (
    "sign in to confirm", "not a bot", "requested format is not available",
    "no video formats found", "format is not available", "timed out", "timeout",
    "temporary failure", "connection reset", "connection aborted", "connection refused",
    "http error 5", "http error 429", "too many requests", "unexpected_eof", "ssl",
    # YouTube 换客户端后有时报这个，不是真的失败，继续试下一个客户端
    "needs to be reloaded",
    # B站 412 是风控：换浏览器 TLS 指纹再试一次（curl_cffi 可用时）
    "http error 412", "precondition failed", "http error 403",
)


def _stream_ytdlp(cmd, job):
    tail = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in proc.stdout:  # type: ignore
            line = line.rstrip("\n")
            tail.append(line)
            del tail[:-40]
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                with JOBS_LOCK:
                    job["progress"] = float(m.group(1))
            if "Merging formats" in line or "Extracting audio" in line:
                with JOBS_LOCK:
                    job["phase"] = "merging"
        proc.wait()
    except Exception:
        try:
            proc.kill()
        except Exception:  # noqa
            pass
        raise
    return proc.returncode, tail


def run_ytdlp(job, job_dir):
    """跑 yt-dlp；失败时按平台策略（客户端切换 / 画质兜底）重试一次。"""
    host = _host_of(job["url"])
    common = [
        "--no-playlist", "--no-overwrites", "--restrict-filenames",
        "--newline", "--no-color",
        "--retries", "5", "--fragment-retries", "10",
        "--extractor-retries", "3", "--socket-timeout", "20",
        "-o", os.path.join(job_dir, "%(title).80s.%(ext)s"),
    ]
    # 视频统一成 mp4；H.264 兼容性由下载后的 ensure_apple_playable() 兜底转码保证。
    if job.get("quality") == "audio":
        common += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        common += ["--merge-output-format", "mp4"]

    try:
        cookies = merged_cookies_path()
    except Exception as e:  # noqa
        cookies = None
        _job_note(job, f"cookies 合并失败，按无 cookies 处理：{e}")
    cookies = cookies_with_visitor(host, cookies, job_dir, job)
    if "bilibili.com" in host:
        cookies = cookies_with_buvid(cookies, job_dir, job)
    if cookies:
        common += ["--cookies", cookies]

    attempts = yt_dlp_attempts(job)
    rc, tail = 0, []
    for i, (label, fmt, extra) in enumerate(attempts):
        if i:
            _job_note(job, f"换策略重试：{label}")
            with JOBS_LOCK:
                job["progress"] = 0.0
                job["phase"] = "downloading"
        cmd = YTDLP + ["-f", fmt] + extra + common + [job["url"]]
        rc, tail = _stream_ytdlp(cmd, job)
        if rc == 0:
            return rc, tail
        low = "\n".join(tail).lower()
        if i + 1 >= len(attempts) or not any(k in low for k in _RETRYABLE):
            break
    return rc, tail


def douyin_download_images(job, job_dir, detail, urls, cookies):
    """下载抖音图文作品的每一张原图。"""
    title = douyin_title(detail)
    n = len(urls)
    saved = []
    with JOBS_LOCK:
        job["phase"] = "downloading"
        job["progress"] = 0.0
    for i, url in enumerate(urls, 1):
        stem = image_stem(title) + (f"_{i:02d}" if n > 1 else "")
        try:
            dest = download_image(url, os.path.join(job_dir, stem), job,
                                  referer="https://www.douyin.com/", cookies_path=cookies)
        except ImageError as e:
            _job_note(job, f"第 {i} 张图片下载失败：{e}")
            dest = None
        if dest:
            saved.append(dest)
        else:
            _job_note(job, f"第 {i} 张不是图片，已跳过")
        with JOBS_LOCK:
            job["progress"] = round(i * 100.0 / n, 1)
    if not saved:
        raise DouyinError("这是抖音图文作品，但一张图片也没下载到（CDN 链接可能已过期），重试一次。")
    with JOBS_LOCK:
        job["title"] = title
    return saved


def douyin_download(job, job_dir, video_id):
    """下载抖音视频/音频/图文，返回产出的文件路径（列表）。"""
    try:
        cookies = merged_cookies_path()
    except Exception as e:  # noqa
        raise DouyinError(f"cookies 合并失败：{e}")
    detail = douyin_fetch_detail(video_id, cookies)
    images = douyin_image_urls(detail)
    if images:
        # 图文作品：把轮播里的图片全部保存下来
        saved = douyin_download_images(job, job_dir, detail, images, cookies)
        if job.get("quality") == "audio":
            _job_note(job, f"这是图文作品（{len(images)} 张图片），没有可提取的音频，已保存全部图片")
        return saved
    url = douyin_pick_url(detail)
    if not url:
        raise DouyinError("没解析到视频地址，可能视频已删除或受限。")
    dest = os.path.join(job_dir, douyin_filename(detail))
    with JOBS_LOCK:
        job["phase"] = "downloading"
        job["progress"] = 0.0
    try:
        _http_to_file(url, dest, job,
                      {"User-Agent": UA_DESKTOP, "Referer": "https://www.douyin.com/"})
    except Exception as e:  # noqa
        raise DouyinError(f"下载抖音视频失败：{e}")
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise DouyinError("抖音 CDN 没有返回视频数据（链接可能已过期），重试一次。")
    if job.get("quality") == "audio":
        with JOBS_LOCK:
            job["phase"] = "merging"
        dest = douyin_to_mp3(dest, job)
    return [dest]


def collect_job_files(job, job_dir, job_id):
    """List the files a job produced (skip .part / .ytdl / our dotfile thumbs)."""
    files = []
    for name in sorted(os.listdir(job_dir)):
        if name.startswith("."):
            continue
        if name.endswith((".part", ".ytdl", ".temp", ".unknown_video", ".unknown_audio")):
            continue
        full = os.path.join(job_dir, name)
        if not (os.path.isfile(full) and os.path.getsize(full) > 0):
            continue
        # 保证视频是 H.264/AAC 的 mp4（iPhone/Safari 能播放和保存）；图片不转码
        if (MOBILE_SAFE and job.get("quality") != "audio" and not is_image_name(name)
                and name.lower().endswith((".mp4", ".m4v", ".mkv", ".webm", ".mov"))):
            with JOBS_LOCK:
                job["phase"] = "converting"
            full = ensure_apple_playable(full, job)
            name = os.path.basename(full)
        if not os.path.isfile(full) or os.path.getsize(full) == 0:
            continue
        files.append({"name": name, "size": os.path.getsize(full),
                      "rel": job_id + "/" + name})
    return files


def run_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return

    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    with JOBS_LOCK:
        job["status"] = "running"
        job["updated"] = now_ms()

    try:
        cookies = merged_cookies_path()
    except Exception as e:  # noqa
        cookies = None
        _job_note(job, f"cookies 合并失败，按无 cookies 处理：{e}")

    tail = []
    removed = removed_platform_message(job["url"])
    if removed:
        _job_failed(job, removed, tail)
        return
    douyin_id = douyin_video_id(job["url"]) if DOUYIN_HOST_RE.match(job["url"]) else ""
    if douyin_id:
        # 抖音走自带下载器：yt-dlp 现在拿不到签名，必然 403
        try:
            douyin_download(job, job_dir, douyin_id)
        except DouyinError as e:
            _job_failed(job, str(e), tail)
            return
        except Exception as e:  # noqa
            _job_failed(job, f"抖音下载出错：{e}", tail)
            return
    else:
        direct_done = False
        # 直链图片：不走 yt-dlp，省一次解析，也避免 "Unsupported URL"
        if looks_like_image_url(job["url"]):
            try:
                dest = direct_image_download(job, job_dir, job["url"], cookies)
            except ImageError as e:
                _job_failed(job, str(e), tail)
                return
            if not dest:
                _job_failed(job, "链接看起来是图片，但服务器返回的不是图片内容："
                                 "可能被防盗链拦截，或该链接需要 cookies 登录。", tail)
                return
            direct_done = True
            tail = [f"直接下载图片：{os.path.basename(dest)}"]
        if not direct_done:
            try:
                rc, tail = run_ytdlp(job, job_dir)
            except Exception as e:  # noqa
                _job_failed(job, str(e), tail)
                return
            if rc != 0:
                low = "\n".join(tail).lower()
                saved = []
                if any(k in low for k in IMAGE_FALLBACK_ERRORS):
                    _job_note(job, "没有视频流，改按图文/图片下载…")
                    try:
                        # 小红书/微博的图文同样需要访客 cookies，补一次再解析
                        fb_cookies = cookies_with_visitor(
                            _host_of(job["url"]), cookies, job_dir, job)
                    except Exception:  # noqa
                        fb_cookies = cookies
                    try:
                        saved = download_images_fallback(job, job_dir, fb_cookies)
                    except ImageError as e:
                        _job_note(job, f"图片兜底失败：{e}")
                if not saved:
                    _job_failed(job, explain_failure(tail, rc), tail)
                    return

    files = collect_job_files(job, job_dir, job_id)
    if not files and not douyin_id:
        # yt-dlp 退出码为 0 却什么都没产出：最后再试一次图文提取
        try:
            fb_cookies = cookies_with_visitor(_host_of(job["url"]), cookies, job_dir, job)
        except Exception:  # noqa
            fb_cookies = cookies
        try:
            download_images_fallback(job, job_dir, fb_cookies)
        except ImageError:
            pass
        files = collect_job_files(job, job_dir, job_id)

    if not files:
        msg = "没有产出文件：下载过程没报错但没有拿到内容，链接可能不是视频/图文页面。"
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = msg
            job["log"] = "\n".join(tail)
            job["finished"] = now_ms()
            job["updated"] = now_ms()
        return

    images = [f for f in files if is_image_name(f["name"])]
    videos = [f for f in files if not is_image_name(f["name"])]

    # 图片任务：缩略图直接用第一张图，多图再打一个 zip 方便一次保存
    thumb, archive, kind = None, None, "video" if videos else "images"
    filename = files[0]["name"]
    if images:
        thumb = make_image_thumb(os.path.join(job_dir, images[0]["name"]),
                                 job_dir, job_id) or images[0]["rel"]
        if len(images) > 1:
            title = (job.get("title") or "").strip()
            archive = make_image_archive(job_dir, job_id, images, title or "images")
            filename = f"{title or '图片'}（{len(images)} 张）"
    if images and videos:
        kind = "mixed"
    # 视频缩略图：<video> 当缩略图在 iOS/Safari 上不会解码，只会显示黑块；
    # 让服务器抽一帧存成 jpg，任何浏览器都能正常显示。
    if not thumb:
        for f in videos:
            if f["name"].lower().endswith((".mp4", ".m4v", ".webm", ".mkv", ".mov")):
                thumb = make_thumb(os.path.join(job_dir, f["name"]), job_dir, job_id)
                break

    with JOBS_LOCK:
        job["status"] = "done"
        job["progress"] = 100.0
        job["files"] = files
        job["thumb"] = thumb
        job["archive"] = archive
        job["kind"] = kind
        job["image_count"] = len(images)
        job["filename"] = filename
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
    if ext in IMAGE_EXTS:
        fallback = "image"
    elif ext == ".zip":
        fallback = "images"
    elif ext in (".mp3", ".m4a", ".aac", ".opus", ".ogg"):
        fallback = "audio"
    else:
        fallback = "video"
    ascii_stem = re.sub(r"_{2,}", "_", ascii_stem)[:150].strip("._-") or fallback
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
            cookie_list = cookie_files()
            try:
                merged = merged_cookies_path()
                _, _, dy_n, dy_ok = _parse_douyin_cookies(merged)
                dy_names = _douyin_cookie_names(merged)
            except Exception:  # noqa
                dy_n, dy_ok, dy_names = 0, False, []
            return self._send_json({"ok": True, "yt_dlp": yt_dlp_version(),
                                    "engine": " ".join(YTDLP),
                                    "ffmpeg": ffmpeg_ok,
                                    "cookies": bool(cookie_list),
                                    "cookies_files": len(cookie_list),
                                    "douyin_cookies": dy_n,
                                    "douyin_uifid": dy_ok,
                                    "douyin_names": dy_names,
                                    "mobile_safe": MOBILE_SAFE,
                                    "proxy": bool(YTDLP_PROXY),
                                    "youtube_proxy": bool(YOUTUBE_PROXY or YTDLP_PROXY),
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
            raw = (body.get("url") or "").strip()
            quality = (body.get("quality") or "best").strip()
            # 允许直接粘贴 App 的分享文案，从里面抠出链接
            url = extract_url(raw)
            if not url:
                return self._send_json(
                    {"error": "没找到链接：请粘贴包含 http(s) 链接的内容（抖音/小红书/B站等的分享文案可以直接粘）"},
                    400)
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
                "thumb": None,
                "archive": None,
                "kind": None,
                "image_count": 0,
                "title": None,
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
        elif low.endswith(".gif"):
            ctype = "image/gif"
        elif low.endswith(".bmp"):
            ctype = "image/bmp"
        elif low.endswith(".avif"):
            ctype = "image/avif"
        elif low.endswith(".zip"):
            ctype = "application/zip"

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
