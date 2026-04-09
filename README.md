# SaveBox

一个简洁实用的社交媒体媒体下载器，支持从 **X (Twitter)**、**YouTube**、**Bilibili** 下载视频、图片和文本内容。

基于 FastAPI + yt-dlp 构建，提供简洁的 Web 界面，无需数据库，开箱即用。

## 功能特性

### X / Twitter

- 视频下载 — 支持多种分辨率选择（1080p / 720p / 480p 等）
- 推文导出 — 将推文文本（含内联图片、展开的链接）保存为 Markdown
- 推串导出 — 一键获取整条推串（最多 100 条），导出为结构化 Markdown 文件
- 支持文本推文和含媒体推文

### YouTube

- 视频下载 — 自动解析可用分辨率和格式
- 字幕烧录 — 可选字幕语言，通过 ffmpeg 将字幕硬烧到视频中
- 支持多种链接格式：`youtube.com/watch`、`youtu.be`、`youtube.com/shorts`、`youtube.com/embed`

### Bilibili

- 视频下载 — 支持多种清晰度选择
- 字幕烧录 — 支持 Bilibili 字幕烧录
- 支持链接格式：`bilibili.com/video/BV*`、`b23.tv/*`

### 通用特性

- 实时下载进度条
- 每个平台独立的代理和 Cookie 配置
- 纯浏览器端操作，无需命令行

## 快速开始

### 环境要求

- Python 3.11+
- ffmpeg（可选，字幕烧录功能需要）

### 安装

```bash
pip install -r requirements.txt
```

### 启动

```bash
python app.py
```

启动后访问 http://localhost:8000 即可使用。

## 使用说明

1. 选择对应平台的标签页（X / YouTube / Bilibili）
2. 粘贴链接到输入框
3. 展开「高级设置」可配置代理地址或 Cookie（可选）
4. 点击「解析」按钮
5. 预览内容信息，选择分辨率和字幕语言
6. 点击「下载」— 进度条实时更新，完成后自动保存到浏览器下载目录

## 高级设置

| 设置项 | 说明 | 示例 |
|--------|------|------|
| 代理 | 支持 HTTP / SOCKS5 代理 | `socks5://127.0.0.1:1080` |
| Cookie | 支持 Netscape 格式或 JSON 数组格式 | 从浏览器开发者工具中复制 |

> Cookie 仅在当前请求中使用，不会持久化存储。

## 项目结构

```
SaveBox/
├── app.py                # 后端主程序：FastAPI 服务、API 路由、下载逻辑
├── requirements.txt      # Python 依赖
├── static/
│   └── index.html        # 前端单页应用（Tailwind CSS + 原生 JS）
├── downloads/            # 临时下载目录（运行时创建，文件传输后自动清理）
└── README.md
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面 |
| POST | `/api/analyze` | 解析 X/Twitter 链接 |
| POST | `/api/download` | 下载 X/Twitter 视频 |
| POST | `/api/article` | 导出单条推文为 Markdown |
| POST | `/api/thread` | 导出整条推串为 Markdown |
| POST | `/api/yt/analyze` | 解析 YouTube 链接 |
| POST | `/api/yt/download` | 下载 YouTube 视频 |
| POST | `/api/bili/analyze` | 解析 Bilibili 链接 |
| POST | `/api/bili/download` | 下载 Bilibili 视频 |
| GET | `/api/progress/{task_id}` | 查询下载进度 |
| GET | `/api/file/{task_id}` | 获取已下载的文件 |

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI |
| ASGI 服务器 | Uvicorn |
| 视频下载引擎 | yt-dlp |
| HTTP 客户端 | requests（Twitter API 调用） |
| 字幕处理 | ffmpeg |
| 前端 | Tailwind CSS + 原生 JavaScript |

## 依赖

```
fastapi >= 0.104.0
uvicorn >= 0.24.0
yt-dlp >= 2024.0.0
python-multipart >= 0.0.6
requests >= 2.31.0
```

## License

MIT
