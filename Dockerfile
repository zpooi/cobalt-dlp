FROM python:3.12-slim

# ffmpeg is required by yt-dlp to merge video+audio
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 可选依赖：yt-dlp 用它模拟浏览器的 TLS 指纹（优酷/爱奇艺等站会拦 Python 的握手）。
# 装不上也不影响构建，程序会自动跳过这个重试策略。
RUN pip install --no-cache-dir curl_cffi || true

COPY app.py index.html selftest.py ./

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
