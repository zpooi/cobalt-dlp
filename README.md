# SaveBox · 服务器下载站（yt-dlp 引擎）

一个极简、好看的网页下载站：**粘贴链接 → 服务器用 yt-dlp 下载 → 页面里直接播放 / 保存**。
下载流量走服务器网络，不经过你本机；适合国内网络访问 Instagram / YouTube / TikTok / X / Bilibili 等。

## 特性
- 支持 Instagram、YouTube、TikTok、X/Twitter、SoundCloud、Pinterest 等（yt-dlp 支持的都行）
- 支持国内平台：**B站、抖音、小红书、微博、优酷、爱奇艺、西瓜视频、今日头条、搜狐、AcFun、知乎、CCTV、QQ音乐、网易云音乐、喜马拉雅、虎牙、斗鱼**
- **分享文案直接粘**：抖音/小红书/B站 App 复制出来的整段文字（带表情、带“复制此链接打开…”）会自动抠出链接，不用手动删
- 页面内 **视频在线播放**（支持拖动进度）+ 一键下载
- 清晰度可选：最佳 / 1080p / 720p / 480p / 仅音频
- 实时进度条、任务列表、失败原因提示
- **文件只保留 1 天**：下载完成的文件在服务器保留 24 小时，到期由后台自动删除，不会长期占用服务器磁盘（可配置）
- **手机可直接播放/保存**：视频统一下载为 mp4，并自动转码为 **H.264 + AAC**（iPhone/Safari/微信/相册原生支持），避免"该格式的视频无法保存"或黑屏
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

## 国内平台说明

| 平台 | 情况 |
|---|---|
| B站（视频/番剧/收藏夹/UP主空间/动态） | ✅ 开箱可用；1080P 以上需要登录 cookies |
| 抖音（含 `v.douyin.com` 短链） | ⚠️ 多数可用；服务器在海外时**大概率需要 cookies**，否则报"无法解析" |
| 小红书（含 `xhslink.com` 短链） | ⚠️ 建议配 cookies（未登录时常被限制） |
| 微博 / 优酷 / 爱奇艺 / 西瓜视频 / 今日头条 / 搜狐 / AcFun / 知乎 / CCTV | ✅ 直接可用 |
| QQ音乐 / 网易云音乐 / 喜马拉雅 | ✅ 直接可用（选"仅音频"） |
| 虎牙 / 斗鱼 直播与回放 | ✅ 直接可用 |
| 快手、腾讯视频(v.qq.com)、芒果TV、咪咕、酷狗 | ❌ yt-dlp 暂不支持 |

**关于 cookies**：`cookies.txt` 是 Netscape 格式，一个文件里可以**同时放多个站**的 cookies（直接拼接即可，yt-dlp 会按域名取用）。抖音建议用浏览器登录后导出，见上一节。

**服务器在海外的影响**：抖音、小红书对非中国大陆 IP 限制较严，这类站点如果报错，先确认 cookies 已配置；B站绝大多数视频海外可下，个别番剧有地区限制。

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

## 手机播放 / 保存兼容性（重要）

iPhone 只认 **H.264(8-bit 4:2:0) + AAC-LC** 的 mp4。很多站点（YouTube、部分抖音/IG 镜像）给的是 **VP9 / AV1 / HEVC**，yt-dlp 照样能把它们**合并进 .mp4 文件**——电脑的 Chrome / VLC 能播，但 iOS 相册、Safari、微信、QQ 全部不认，表现就是：

- 页面里点播放：黑屏，或只有声音没有画面
- 下载到"文件"App：显示 MPEG-4 影片但打开是黑的
- 传到 QQ/微信：提示"不支持的格式 / 该格式的视频无法保存"
- **电脑上能正常播放，手机上不能** —— 这就是典型特征

本项目下载后会**真正读取视频流编码**（ffprobe），只要不是手机原生支持的组合就自动用 ffmpeg 转成 H.264/AAC，转完再复检一遍；任务卡片上会显示 `转码为 H.264/AAC：视频编码是 vp9…` 或 `已是 H.264/AAC，无需转码`。

- 转码需要时间（10 秒的 1080p60 片子约十几秒），期间状态显示"转码中…"。
- 需要服务器装有 `ffmpeg` **和** `ffprobe`（Docker 镜像自带；`/api/health` 的 `ffmpeg` 字段可确认）。
- `MOBILE_SAFE=0` 可关闭强制转码。
- `TRANSCODE_MAX_HEIGHT=1080` 可在转码时限制分辨率（4K 源转 1080p 更快、手机更流畅）；默认 `0` 保持原分辨率。

## 常见问题
- **手机提示"该格式的视频无法保存" / 网页/手机播放黑屏 / 只有声音**：见上一节。**旧文件不会自动重转，请重新下载一次。**
- **下载到手机后文件名变成 `.mp4` 或者没有后缀、无法识别**：已修复。现在下载名一定会保留正确后缀（纯中文标题会退化为 `video.mp4`，同时仍通过 `filename*` 显示原中文名）。
- **下载失败 "login required" / "rate-limit"**：配置 cookies（见上）。
- **下载失败 "unable to extract"**：目标站改版或链接非公开，先更新 yt-dlp：`docker compose exec savebox pip install -U yt-dlp` 后 `docker compose restart`。
- **清晰度没有 1080p**：源本身最高就那么多；选"最佳画质"即可。
- **文件太大/带宽**：下载存服务器磁盘，注意磁盘空间（`downloads/` 可定期清理）。
