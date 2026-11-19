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
# 把你的 cookies.txt（或按站分开的多个 .txt）放到 cookies/ 目录

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

cookies 是 Netscape 格式的文本文件（浏览器扩展导出的就是）。

**方式一：浏览器扩展（最简单）**
1. Chrome/Edge 安装扩展 **"Get cookies.txt LOCALLY"**
2. 登录 instagram.com（或 youtube.com）
3. 点扩展 → Export → 保存为 `cookies.txt`
4. 放进 `cobalt-dlp/cookies/` 目录即可（Docker）。**不用改名、不用合并**：目录里每个 `*.txt` 都会被自动合成一份 cookies 交给 yt-dlp，比如 `cookies.txt`、`www.douyin_com_cookies.txt`、`bilibili.txt` 可以同时放着
5. 重启：`docker compose restart`

**方式二：yt-dlp 直接读浏览器（仅当服务器上有该浏览器会话时）**
一般服务器没有浏览器，推荐方式一。

> 安全提示：cookies 等于你的登录凭证，勿泄露、勿提交到公开仓库。本项目已通过 `.gitignore` 排除。

## 目录结构
```
cobalt-dlp/
├── app.py                # 后端（HTTP 服务 + 任务队列 + yt-dlp 调用）
├── index.html            # 前端页面
├── selftest.py           # 逐平台自检脚本
├── requirements.txt      # yt-dlp
├── Dockerfile
├── docker-compose.yml
├── run.sh                # 非 Docker 启动
├── downloads/            # 下载的文件（Docker 挂载到宿主机）
└── cookies/              # 放 cookies（目录里所有 *.txt 自动合并）
```

## 各平台支持与自动适配

| 平台 | 状态 | 需要 cookies？ |
|---|---|---|
| B站（视频/番剧/收藏夹/UP主空间/动态） | ✅ 可用 | 默认不需要（自动补 buvid3）；1080P 以上需要登录 cookies |
| 抖音（含 `v.douyin.com` 短链） | ✅ 内置下载器 | **必须**：带 `UIFID` 的 douyin cookies；图文作品暂不支持 |
| 微博 / 今日头条 / 搜狐 / AcFun / CCTV | ✅ 可用 | 一般不需要 |
| QQ音乐 / 网易云音乐 / 喜马拉雅 | ✅ 可用（选"仅音频"） | 不需要 |
| 虎牙（直播/回放） | ✅ 可用 | 不需要 |
| 小红书（含 `xhslink.com` 短链） | ⚠️ 仅视频笔记 | 建议配 cookies；图文笔记不支持 |
| 优酷 / 爱奇艺 / 西瓜视频 / 斗鱼 | ⚠️ 视内容 | yt-dlp 提取器对部分内容失效；失败时页面会说明原因，建议配 cookies |
| 知乎 | ⚠️ 视内容 | yt-dlp 无自带测试链接，未验证 |
| YouTube | ✅ 可用 | 一般不需要（自动切客户端）；被判定"机器人"时需要 |
| Instagram / TikTok / X（Twitter） | ⚠️ 视内容 | 多数内容需要 |
| SoundCloud / Pinterest | ✅ 可用 | 不需要 |
| Facebook / Reddit / Vimeo 等 | ✅ yt-dlp 支持的都行 | 视内容 |
| 快手、腾讯视频(v.qq.com)、芒果TV、咪咕、酷狗 | ❌ 暂不支持 | — |

> 表里的 ✅/⚠️ 是通过 `selftest.py` 用 yt-dlp **自带测试链接**跑出来的结果。测试链接可能过期，所以 ⚠️ 不代表真实链接一定失败；遇到失败时看页面给的原因。

**程序会在每个任务里自动做的事**（不用手动配置）：

- **cookies 自动合并**：`cookies/` 下所有 `.txt`（Netscape 或 JSON 格式）按修改时间从旧到新合并，同站点的重复 cookie 以最新导出为准
- **B站 412**：自动向 bilibili 要一对访客 ID（`buvid3`/`buvid4`）补进 cookies——机房 IP 不加这个必 412
- **小红书 / 微博**：没配 cookies 时自动去要首页下发的访客 cookie（`a1`/`SUB` 这类关键 cookie 登录才有）
- **YouTube 机器人校验**：自动依次尝试默认 → `tv` 客户端 → `web_safari` 客户端，三种都失败才报错
- **画质兜底**：选了 1080p 但源最高只有 720p 时，自动退回"最佳可用画质"，而不是直接失败
- **网络重试**：`--retries 5 --fragment-retries 10 --extractor-retries 3`，超时/5xx/断流会自动再试
- **iOS 兼容兜底**：下载后统一用 ffprobe 复检，非 H.264/AAC 自动转码（见下文）

**关于 cookies**：按站分开存成多个文件最省事（`www.douyin_com_cookies.txt`、`bilibili.txt`…），替换或新增后直接重新下载即可，**不用重启容器**。单个文件里放多个站也照样支持。

**抖音为什么不用 yt-dlp**：yt-dlp 官方的 Douyin 提取器会被抖音 Argus 网关直接 403（`Uifid Not Found` / `Signature Not Found`），加什么 cookies 都一样。本项目内置了抖音下载器：先从 `v.douyin.com` 短链拿到视频 ID，按 `x-secsdk-web-signature` 算法给 `aweme/detail` 接口签名（`uifid + timestamp + 固定盐值` 的 MD5），请求头再带上 `uifid`，从而拿到视频地址。所以抖音**必须有 cookies**，且文件里要包含 `UIFID`（或 `UIFID_TEMP`）；缺了会直接提示重新导出。选"仅音频"时下载视频后用 ffmpeg 转 mp3。

**服务器在海外的影响**：抖音、小红书对非中国大陆 IP 限制较严；抖音的签名请求专门针对机房 IP 设计，B站加了 buvid 后一般也能过。YouTube 在部分机房 IP 会触发"确认你不是机器人"，此时配 cookies 最稳。

### 逐平台自检

部署后想确认每个站现在能不能用：

```bash
docker compose exec savebox python selftest.py              # 全部平台
docker compose exec savebox python selftest.py B站 YouTube  # 只测几个
```

脚本用 yt-dlp 各提取器自带的测试链接跑 `--simulate`（不下载），抖音则直接调内置下载器的接口，输出一张"平台 / 是否可用 / 失败原因"的表。失败的那行原因可以直接贴出来排查。

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
- **失败原因看不明白**：失败卡片现在会直接给中文原因（例如"抖音要求 cookies…"），点下面的「yt-dlp 输出」可以展开原始日志。
- **下载失败 "login required" / "rate-limit" / "Fresh cookies … are needed"**：配置 cookies（见上）。抖音的这条报错几乎都是缺 cookies。
- **下载失败 "unable to extract"**：目标站改版或链接非公开，先更新 yt-dlp：`docker compose exec savebox pip install -U yt-dlp` 后 `docker compose restart`。
- **清晰度没有 1080p**：源本身最高就那么多；选"最佳画质"即可。
- **文件太大/带宽**：下载存服务器磁盘，注意磁盘空间（`downloads/` 可定期清理）。
