#!/usr/bin/env python3
"""逐平台自检：确认每个支持的站点当前还能不能解析。

用法（在服务器上）：
    docker compose exec savebox python selftest.py            # 全部平台
    docker compose exec savebox python selftest.py B站 YouTube  # 只测指定平台

每个平台用 yt-dlp 自带测试链接跑 --simulate（不真正下载），最多试 3 条
链接（自带测试链接经常早就失效，只测一条会误判），抖音走项目内置下载器
的接口（不需要视频文件）。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import app
from yt_dlp.extractor import gen_extractors

PLATFORMS = [
    ("B站", ["BiliBili"]),
    ("抖音", ["Douyin"]),
    ("小红书", ["XiaoHongShu"]),
    ("微博", ["Weibo"]),
    ("西瓜视频", ["Ixigua"]),
    ("今日头条", ["Toutiao"]),
    ("搜狐", ["Sohu"]),
    ("AcFun", ["AcFunVideo"]),
    ("CCTV", ["CCTV"]),
    ("QQ音乐", ["QQMusic"]),
    ("网易云", ["NetEaseMusic"]),
    ("喜马拉雅", ["Ximalaya"]),
    ("虎牙", ["HuyaVideo"]),
    ("YouTube", ["Youtube"]),
    ("Instagram", ["Instagram"]),
    ("TikTok", ["TikTok"]),
    ("X/Twitter", ["Twitter"]),
    ("SoundCloud", ["Soundcloud"]),
    ("Pinterest", ["Pinterest"]),
]

TIMEOUT = 75


def _has_curl_cffi():
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:  # noqa
        return False


def find_ie(keys):
    by_key = {ie.ie_key(): ie for ie in gen_extractors()}
    for k in keys:
        for key, ie in by_key.items():
            if key.lower() == k.lower():
                return ie
    for k in keys:
        for key, ie in by_key.items():
            if k.lower() in key.lower():
                return ie
    return None


def test_urls(ie, limit=3):
    """取前几个可用的自带测试链接。

    单条测试链接经常早就失效（被删、账号注销、amplify 卡片域名过期），
    只测第一条会把能用的平台误判成失败，所以按顺序试几条，成功一条即算通过。
    """
    out = []
    for t in getattr(ie, "_TESTS", None) or []:
        if t.get("skip") or t.get("only_matching"):
            continue
        u = t.get("url")
        if isinstance(u, str) and u.startswith("http") and ".onion" not in u:
            if u not in out:
                out.append(u)
        if len(out) >= limit:
            break
    return out


def prepare_cookies(workdir):
    try:
        cookies = app.merged_cookies_path()
    except Exception:  # noqa
        cookies = None
    return app.cookies_with_buvid(cookies, workdir)


def _probe_once(url, cookies, fmt, extra):
    cmd = [sys.executable, "-m", "yt_dlp", "--simulate", "--no-playlist",
           "--no-warnings", "--socket-timeout", "15", "--retries", "2",
           "-f", fmt, "-O", "%(title).40s"] + list(extra)
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "超时"
    if r.returncode == 0:
        return True, (r.stdout or "").strip().splitlines()[-1][:60] if r.stdout.strip() else "OK"
    for line in reversed((r.stderr or "").splitlines()):
        if line.startswith("ERROR:"):
            return False, line[6:].strip()[:110]
    return False, ((r.stderr or "").strip().splitlines() or ["失败"])[-1][:110]


def run_probe(url, cookies):
    """和正式下载用同一套策略（YouTube 客户端切换、画质兜底）。

    返回 (ok, 最后一条说明, 每次尝试的 (策略, 结果))，失败时把每一轮的原因
    都打印出来，方便判断是"所有客户端都被拦"还是"某个客户端拿到了格式"。
    """
    job = {"url": url, "quality": "best"}
    last = ""
    tried = []
    for label, fmt, extra in app.yt_dlp_attempts(job):
        ok, info = _probe_once(url, cookies, fmt, extra)
        tried.append((label, info))
        if ok:
            return True, info, tried
        last = info
        if not any(k in info.lower() for k in app._RETRYABLE):
            break
    return False, last, tried


def youtube_cookie_state():
    """判断合并后的 cookies 里是否有 YouTube 的登录态（只看名字，不外泄值）。"""
    try:
        path = app.merged_cookies_path()
    except Exception:  # noqa
        return "无"
    if not path:
        return "无"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return "无"
    names = set()
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            names.add(parts[5])
    if not names:
        return "无"
    logged = names & {"SID", "HSID", "SSID", "APISID", "SAPISID",
                      "LOGIN_INFO", "__Secure-1PSID", "__Secure-3PSID"}
    if "LOGIN_INFO" in names or "SID" in names:
        return f"已登录（{len(names)} 个，含 SID/LOGIN_INFO）"
    if logged:
        return f"疑似已登录（有 {len(logged)} 个登录 cookie，但缺 SID/LOGIN_INFO）"
    return f"未登录（{len(names)} 个 cookie，没有 SID/LOGIN_INFO）"


def probe_douyin():
    vid = "6961737553342991651"
    try:
        cookies = app.merged_cookies_path()
        detail = app.douyin_fetch_detail(vid, cookies)
    except app.DouyinError as e:
        return False, str(e)[:110]
    except Exception as e:  # noqa
        return False, str(e)[:110]
    url = app.douyin_pick_url(detail)
    return (True, (detail.get("desc") or "")[:60]) if url else (False, "没有视频地址")


def main(argv):
    only = {a.lower() for a in argv}
    workdir = tempfile.mkdtemp(prefix="selftest-")
    cookies = prepare_cookies(workdir)
    import yt_dlp.version as ytv
    print(f"环境：yt-dlp {ytv.__version__} | JS 运行时：{'deno' if shutil.which('deno') else '无'}"
          f" | curl_cffi：{'有' if _has_curl_cffi() else '无'}"
          f" | YouTube cookies：{youtube_cookie_state()}"
          f" | 代理：{'YouTube 专用' if app.YOUTUBE_PROXY else ('全局' if app.YTDLP_PROXY else '无')}")
    rows = []
    print("平台            结果   说明")
    print("-" * 78)
    for name, keys in PLATFORMS:
        if only and name.lower() not in only and not any(k.lower() in only for k in keys):
            continue
        tried = []
        if name == "抖音":
            ok, info = probe_douyin()
        else:
            ie = find_ie(keys)
            if not ie:
                ok, info = False, "yt-dlp 没有这个提取器"
            else:
                urls = test_urls(ie)
                if not urls:
                    ok, info = None, "提取器没有自带测试链接，跳过"
                else:
                    ok, info = False, "没有可用的测试链接"
                    for url in urls:
                        ok, info, tried = run_probe(url, cookies)
                        if ok:
                            break
        rows.append((name, ok, info))
        mark = "✅ 可用" if ok else ("❌ 失败" if ok is False else "⚠️ 跳过")
        print(f"{name:<14} {mark:<6} {info}")
        if ok is False and tried:
            for label, msg in tried:
                print(f"    · {label}: {msg[:100]}")
    ok_n = sum(1 for _, ok, _ in rows if ok)
    bad_n = sum(1 for _, ok, _ in rows if ok is False)
    print("-" * 78)
    print(f"合计 {len(rows)} 个平台，可用 {ok_n} 个，失败 {bad_n} 个，跳过 {len(rows) - ok_n - bad_n} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
