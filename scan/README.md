# Video Analyzer（scan@dk · 离线化重构版）

本地 Web 工具：粘贴 **Bilibili** 视频 URL → 字幕提取 → AI 分析 → Obsidian 集成 → RAG 对话问答。

> **与旧版（`D:\project\claude\scan`）的核心差异（P0 重构）**：
> 1. **识别主链路 = B 站官方 AI 字幕**（`x/player/wbi/v2`，`ai_status==2`）——纯 HTTP、国内普通网络直连、**零模型下载、零 GPU**；
> 2. **启动零外网**：Whisper / bge-m3 / bge-reranker 全部本地化（`models/`），懒加载 + 自检，断网可启动；
> 3. 模型经 **ModelScope（魔搭）/ hf-mirror** 一次性下载（国内直连），不再依赖 HuggingFace/代理；
> 4. YouTube 代码保留但普通网络下不可达（不做投入）。
>
> 完整计划见 [`docs/plan/`](docs/plan/)。

---

## 快速开始

```bash
cd D:\project\dk\scan
pip install -r requirements.txt            # 首次
pip install modelscope                      # 模型下载需要（或 pip install -e ".[download]")
python scripts/download_models.py --all     # 首次：国内直连下载模型（之后可断网）
python scripts/check_offline.py             # 自检：全部就绪 → 可断网启动
taskkill //F //IM python.exe 2>nul          # 停旧进程
python -c "import uvicorn; uvicorn.run('src.web.server:app', host='127.0.0.1', port=8787)"
```

浏览器打开 `http://127.0.0.1:8787`，粘贴 B 站视频 URL，点击解析。

---

## 前置依赖

| 依赖 | 用途 | 网络要求 |
|------|------|------|
| Python 3.12+ | 运行环境 | — |
| DeepSeek API Key | LLM 分析 (`.env`) | 运行时联网（启动不需要） |
| 本地模型（`models/`） | Whisper 兜底转录 / RAG 嵌入+重排 | 仅首次下载需普通网络 |
| CUDA Toolkit 12+（可选） | GPU 加速 Whisper | 无 GPU 自动用 CPU |

## 配置

`.env` 文件（模板见 `.env.example`）：

```env
ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-flash[1m]

# Obsidian Vault 集成（选填，不填则输出到项目 output/ 目录）
OUTPUT_DIR=D:\tool\ob\know\0604\视频笔记
OBSIDIAN_VAULT=D:\tool\ob\know\0604

# Bilibili cookie 文件（遇 412 错误时配置，选填）
# BILIBILI_COOKIES=bilibili_cookies.txt

# 本地模型目录（默认 models/）
# MODEL_DIR=models
```

---

## 识别链路（B 站，三级）

```
1. CC 字幕（player/v2）── 命中即用，无需模型
2. AI 字幕（player/wbi/v2，ai_status==2）── ★主链路：B站服务端已转写，秒级、免费、零模型
3. faster-whisper 兜底（本地 models/whisper-small，CPU/GPU 自动检测）── 仅无 CC 无 AI 的视频
```

- 字幕来源会在进度状态与结果中标注（`AI字幕(ai-zh@wbi/v2)` / `CC字幕(...)` / `whisper`）
- 音频下载走 B 站 playurl API 直连（绕开 yt-dlp 的 412），见 `src/transcriber.py`

## 模型下载

| 模型 | 用途 | 体积 | 国内源 |
|------|------|:--:|------|
| `faster-whisper-small` | 兜底转录 | ~460MB | ModelScope `pengzhendong/faster-whisper-small`；备选 hf-mirror |
| `BAAI/bge-m3` | RAG 嵌入 | ~2.2GB | ModelScope `BAAI/bge-m3` |
| `BAAI/bge-reranker-v2-m3` | RAG 精排 | ~1.2GB | ModelScope `BAAI/bge-reranker-v2-m3` |

```bash
python scripts/download_models.py --all          # 全部
python scripts/download_models.py --whisper      # 只下 Whisper
```

**离线保证**：模型就绪后（`check_offline.py` 全绿），启动服务与 B 站字幕识别完全不需要外网；仅 LLM 分析（DeepSeek API）和首次模型下载需要网络。

---

## 功能

- **单视频解析**：字幕/概览/重点/思维导图，输出 `.md ×3 + .srt + meta.json + mindmap.md`
- **批量解析**：B 站合集 list → 串行逐个分析，自动归入 `作者_日期` 子文件夹，失败计数
- **RAG 问答**：单视频追问（多轮）+ 跨视频全局搜索，bge-m3 嵌入 + cross-encoder 重排
- **Obsidian 集成**：YAML frontmatter + `[[wikilink]]` 概念 + Vault 直写
- **导出**：HTML 自包含 / PDF（weasyprint→Chrome 双后端）/ ZIP 批量

## API 端点

见旧版 README 同构（`/api/analyze`、`/api/analyze/playlist`、SSE 进度、`/api/history`、`/api/chat`、`/api/compare`、`/api/export/*` 等）；P3 将新增 `/api/result/{task_id}` 轮询与 `/api/summary/{task_id}` 文本摘要（QQ bot 契约，见 `docs/plan/03`）。

## 项目结构

```
scan/
├── src/
│   ├── config.py            # 配置（含离线化：model_dir/embedding_model/rerank_model）
│   ├── models.py            # 数据契约（VideoMeta 含 subtitle_source）
│   ├── extractors/
│   │   ├── bilibili.py      # ★AI 字幕优先 + 官方 WBI 签名 + playurl 音频
│   │   └── youtube.py       # 保留（普通网络不可达）
│   ├── transcriber.py       # Whisper 本地模型（models/），GPU/CPU 自动检测
│   ├── rag/
│   │   ├── vectorstore.py   # ChromaDB 懒加载 + 本地嵌入 + 遥测关闭
│   │   └── reranker.py      # 本地重排模型
│   └── web/                 # FastAPI + SSE + 单页前端
├── scripts/
│   ├── download_models.py   # 国内多源模型下载（幂等）
│   └── check_offline.py     # 离线就绪自检（--json 供部署脚本）
├── models/                  # 本地模型（gitignore，经脚本下载）
├── docs/plan/               # 重构计划文档（v2 含 hermes/qqbot 方案）
└── 迭代文档/                 # 旧项目经验沉淀（问题记录/经验总结）
```

## 注意事项

- **Bilibili 412**：UA 轮换 + WBI 签名 + cookie 支持 + playurl 直连已解决；云服务器/数据中心 IP 下务必配置 `BILIBILI_COOKIES`
- **AI 字幕可用性（实测 2026-07）**：住宅网络通常无需 cookie 即可拿到 `ai_status==2` 的 AI 字幕；**数据中心/风控 IP 下 wbi/v2 会返回空字幕列表**（实测热门视频 20/20 为空），此时必须配置 `BILIBILI_COOKIES`，否则自动降级走 Whisper 兜底
- **WBI 签名**：nav 接口未登录（code=-101）时仍返回 `wbi_img`，代码按字段在场与否取密钥（不能按 code 判断），无 cookie 也能完成签名
- **模型未下载时**：服务降级启动，日志/错误提示会给出 `python scripts/download_models.py --xxx` 指引
- **长视频**：无 AI 字幕的长视频走 CPU Whisper 较慢（30min 视频约 1–2.5h），建议配置 `WHISPER_MODEL=medium` 或 GPU
- **端口占用**：启动前 `taskkill //F //IM python.exe` 清理残留进程

## License

MIT
