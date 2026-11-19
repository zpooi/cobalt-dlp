# SaveBox · 服务器下载站（yt-dlp 引擎）

一个极简、好看的网页下载站：**粘贴链接 → 服务器用 yt-dlp 下载 → 页面里直接播放 / 保存**。
下载流量走服务器网络，不经过你本机；适合国内网络访问 Instagram / YouTube / TikTok / X / Bilibili 等。

## 特性
- 支持 Instagram、YouTube、TikTok、X/Twitter、Bilibili、SoundCloud、Pinterest 等（yt-dlp 支持的都行）
- 页面内 **视频在线播放**（支持拖动进度）+ 一键下载
- 清晰度可选：最佳 / 1080p / 720p / 480p / 仅音频
- 实时进度条、任务列表、失败原因提示
- **文件只保留 1 天**：下载完成的文件在服务器保留 24 小时，到期由后台自动删除，不会长期占用服务器磁盘（可配置）
- **支持 cookies**（需登录的内容、避免 IG/YT 限流）
- 纯 Python 标准库 + yt-dlp，无重依赖

## 用 Docker 部署（推荐）

```bash
# 1. 把整个 cobalt-dlp 目录传到服务器，进入目录
cd cobalt-dlp

# 2. (可选) 放 cookies，支持登录内容
mkdir -p cookies
# 把你的 cookies.txt 放到 cookies/cookies.txt

# 3. 启动
docker compose up -d --build

# 4. 查看
docker compose logs -f
```

浏览器打开 **`http://服务器IP:12003`**（记得在云安全组 / ufw 放行 12003）。

> 端口在 `docker-compose.yml` 里，`ports: "12003:13000"` 左边 12003 是宿主机端口，可自行改。

## 不用 Docker（直接跑）

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
./run.sh
# 浏览器打开 http://服务器IP:13000
```

## 如何导出 cookies（支持登录 / 避免限流）

cookies 是一个 Netscape 格式的 `cookies.txt`。

**方式一：浏览器扩展（最简单）**
1. Chrome/Edge 安装扩展 **"Get cookies.txt LOCALLY"**
2. 登录 instagram.com（或 youtube.com）
3. 点扩展 → Export → 保存为 `cookies.txt`
4. 放到 `cobalt-dlp/cookies/cookies.txt`（Docker）或项目根目录 `cookies.txt`（直接跑）
5. 重启：`docker compose restart`

**方式二：yt-dlp 直接读浏览器（仅当服务器上有该浏览器会话时）**
一般服务器没有浏览器，推荐方式一。

> 安全提示：cookies 等于你的登录凭证，勿泄露、勿提交到公开仓库。本项目已通过 `.gitignore` 排除。

## 目录结构
```
cobalt-dlp/
├── app.py                # 后端（HTTP 服务 + 任务队列 + yt-dlp 调用）
├── index.html            # 前端页面
├── requirements.txt      # yt-dlp
├── Dockerfile
├── docker-compose.yml
├── run.sh                # 非 Docker 启动
├── downloads/            # 下载的文件（Docker 挂载到宿主机）
└── cookies/              # 放 cookies.txt
```

## 文件保留（自动清理）

下载完成的文件**默认只保留 1 天（24 小时）**，到期后由后台线程自动删除（文件 + 任务记录），不会长期占用服务器磁盘。页面每张已完成卡片会显示"剩余保留时间"。

- 保留时长用环境变量 `RETENTION_HOURS` 控制（默认 `24`，设为 `0` 表示不自动删除、永久保留）。
- 后台清理扫描间隔用 `CLEANUP_INTERVAL`（秒，默认 `600`）。进程启动时也会先清一次遗留的过期文件。
- 正在下载中的任务不会被清理；从任务**完成/失败**的时刻开始计时。
- Docker 用户改 `docker-compose.yml` 里的 `RETENTION_HOURS` 即可。

```yaml
environment:
  RETENTION_HOURS: "24"    # 改成 12 就是保留 12 小时，0 = 永久
```

## 常见问题- **下载失败 "login required" / "rate-limit"**：配置 cookies（见上）。
- **下载失败 "unable to extract"**：目标站改版或链接非公开，先更新 yt-dlp：`docker compose exec savebox pip install -U yt-dlp` 后 `docker compose restart`。
- **清晰度没有 1080p**：源本身最高就那么多；选"最佳画质"即可。
- **文件太大/带宽**：下载存服务器磁盘，注意磁盘空间（`downloads/` 可定期清理）。
