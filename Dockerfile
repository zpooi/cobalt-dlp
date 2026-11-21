FROM python:3.12-slim

# ffmpeg is required by yt-dlp to merge video+audio
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp 2026 起需要一个 JS 运行时（推荐 Deno）来解 YouTube 的 nsig 挑战；
# 没有的话 YouTube 会少格式、也更容易被判定成机器人。
# 下载失败不影响构建，只是 YouTube 能力变弱。
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) asset=deno-x86_64-unknown-linux-gnu.zip ;; \
      arm64) asset=deno-aarch64-unknown-linux-gnu.zip ;; \
      *) asset="" ;; \
    esac; \
    if [ -n "$asset" ]; then \
      (curl -fsSL "https://github.com/denoland/deno/releases/latest/download/$asset" -o /tmp/deno.zip \
       && unzip -o /tmp/deno.zip -d /usr/local/bin \
       && rm -f /tmp/deno.zip \
       && deno --version) || echo '[!] deno 安装失败，YouTube 部分格式可能不可用'; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 可选依赖：yt-dlp 用它模拟浏览器的 TLS 指纹（部分站点会拦 Python 的握手）。
# 装不上也不影响构建，程序会自动跳过这个重试策略。
RUN pip install --no-cache-dir curl_cffi || true

COPY app.py douyin_abogus.py index.html selftest.py ./

ENV PORT=13000 \
    HOST=0.0.0.0 \
    DOWNLOAD_DIR=/downloads \
    COOKIES_FILE=/cookies/cookies.txt \
    RETENTION_HOURS=24 \
    CLEANUP_INTERVAL=600 \
    MOBILE_SAFE=1 \
    TRANSCODE_MAX_HEIGHT=0

RUN mkdir -p /downloads /cookies

EXPOSE 13000
CMD ["python", "app.py"]
