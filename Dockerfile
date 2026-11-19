FROM python:3.12-slim

# ffmpeg is required by yt-dlp to merge video+audio
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./

ENV PORT=13000 \
    HOST=0.0.0.0 \
    DOWNLOAD_DIR=/downloads \
    COOKIES_FILE=/cookies/cookies.txt \
    RETENTION_HOURS=24 \
    CLEANUP_INTERVAL=600 \
    MOBILE_SAFE=1

RUN mkdir -p /downloads /cookies

EXPOSE 13000
CMD ["python", "app.py"]
