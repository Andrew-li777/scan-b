# 03 hermes（云服务器智能体）+ QQ bot 部署探究

> 目标：G3 —— 重构跑通后，部署到**云服务器上的智能体 hermes**，用户通过 **QQ bot** 调用本服务。
> 需求澄清（v2）：hermes 不是裸服务器，而是**云服务器上运行的智能体**；调用入口是 QQ bot。
> 结论先行：**可行**，核心前提是 P0 的"AI 字幕优先"改造——它让 CPU-only 云服务器上"识别"从分钟级降为秒级。

---

## 一、调用链拓扑

```
QQ 用户 ──消息──▶ QQ bot 框架（NoneBot2 / OneBot-NapCat / Lagrange，hermes 上运行）
                        │  HTTP（Bearer token）
                        ▼
              scan 服务（uvicorn :8787，与 bot 同机或另一台机器）
                        │  api.bilibili.com / aisubtitle.hdslb.com（国内直连）
                        │  api.deepseek.com（LLM 分析，运行时）
                        ▼
              models/（本地 whisper/bge，离线） + output/ + .chromadb/
```

| 拓扑选项 | 说明 | 适用 |
|------|------|------|
| A. bot 与 scan 同机 | bot 走 `http://127.0.0.1:8787`，scan 只监听内网 | 推荐：简单、无暴露面 |
| B. 分离部署 | scan 独立一台，bot 走 `http://<scan-ip>:8787` | 两台云主机/负载分离时 |

**项目边界**：本项目交付 scan 服务的 HTTP API（契约见 `docs/api-contract.md`）+ 最小适配器示例（`deploy/hermes/qqbot_adapter.py`）；QQ bot 框架选型与维护归 hermes 侧。

---

## 二、可行性分析

### 2.1 为什么可行

| 组件 | 云服务器（无 GPU）状态 | 说明 |
|------|------|------|
| **字幕获取（CC + AI 字幕）** | ✅ 完全不受影响 | 纯 HTTP。重构后是主链路，QQ 场景下绝大多数视频走此路 |
| **faster-whisper 兜底** | ⚠️ 可用但慢 | CPU small int8，无 CUDA 依赖；仅无字幕视频触发 |
| **音频下载（playurl）** | ✅ 不受影响 | 纯网络 |
| **LLM 分析（DeepSeek API）** | ✅ 不受影响 | 云端推理 |
| **嵌入/重排（bge-m3 等）** | ⚠️ 较慢但可接受 | CPU 推理，懒加载；QQ 问答场景单次查询 10s 内 |
| **Web UI** | ✅ 保留 | 降级为管理/历史浏览端，不再是主要入口 |

### 2.2 CPU 性能预算（云服务器 2–4 vCPU 参考）

| 任务 | 耗时估算 | QQ 场景影响 |
|------|------|------|
| B 站 AI/CC 字幕 | 1–3 秒 | 理想：用户发链接 → 数十秒内出摘要 |
| Whisper small int8 转录 | 0.2–0.5× 实时（30min 视频 ≈ 1–2.5h） | **需在 bot 侧管理预期**：先回"正在解析（无字幕视频，预计较久）"，完成后再主动推送 |
| 嵌入/重排 | 秒级 | 追问延迟可接受 |
| LLM 摘要/重点 | 5–40s（长视频 Map-Reduce） | 主导耗时；bot 先回"解析中"再推送 |

---

## 三、API-first 设计约束（新增端点）

| 端点 | 方法 | 用途 | 关键设计 |
|------|------|------|------|
| `/api/analyze?url=` | POST | 触发分析（现有） | 立即返回 `task_id`；**鉴权必填** |
| `/api/result/{task_id}` | GET | **bot 轮询结果**（新增 3.1） | `{"status":"running\|queued\|done\|error", ...}`；done 负载与 SSE 一致 |
| `/api/summary/{task_id}` | GET | **QQ 文本摘要**（新增 3.2） | 纯文本/Markdown，无表格；截断到 QQ 长度限制 |
| `/api/chat` | POST | RAG 追问（现有） | bot 透传问题即可 |
| `/api/history` | GET | 历史列表（现有） | 供 bot 命令如"最近分析" |

**约定**：
- 鉴权：`Authorization: Bearer <AUTH_TOKEN>`；`AUTH_TOKEN` 未配置时跳过（本地/内网模式）
- 异步模型：所有耗时任务先返回 `task_id`，bot 按 2–5s 间隔轮询 `/api/result`；不做同步阻塞端点（防止 bot 框架超时）
- 队列：并发闸（转录 ≤1、LLM ≤3）满载时返回 `{"status":"queued"}`，bot 提示用户排队
- 错误：统一 `{"status":"error","message":"人话中文"}`，bot 可直接转发

---

## 四、云服务器部署形态

### 4.1 推荐：venv + systemd（同 03 v1，路径换为云主机标准布局）

```
/opt/scan
├── src/  output/  models/  .chromadb/  .cache/  tempvideo/
├── .env            # DEEPSEEK key + BILIBILI_COOKIES + AUTH_TOKEN
├── venv/
└── deploy/hermes/scan.service
```

systemd 要点：`WorkingDirectory=/opt/scan`、`EnvironmentFile=/opt/scan/.env`、`Restart=on-failure`、`User=scan`（非 root）、监听 `127.0.0.1:8787`（bot 同机）或 `0.0.0.0`（分离部署时，配合防火墙只放行 bot 来源 IP）。

### 4.2 依赖裁剪

- `requirements-server.txt`：去 `nvidia-cublas-cu12`；`faster-whisper` CPU int8 可用；weasyprint 可选（QQ 场景几乎不用 PDF，可去）
- 模型一次性下载：`python scripts/download_models.py --all`（ModelScope 国内直连，数分钟）

---

## 五、云服务器特有的风险与对策

| 风险 | 等级 | 对策 |
|------|:--:|------|
| **云主机 IP 的 B 站风控**（数据中心 IP 比住宅 IP 更容易 412/验证码） | **高** | ① 优先 AI 字幕主链路（API 层已有 UA 轮换 + WBI 签名 + playurl 直连）；② **必须配置 `BILIBILI_COOKIES`**（浏览器导出，定期更新）；③ 批量串行 + 请求退避（已有）；④ 部署后先跑 T22 连续解析观察失败率 |
| 无鉴权暴露 | 高 | `AUTH_TOKEN` 必配；分离部署时防火墙限制来源 IP |
| CPU 转录长任务 | 中 | 并发闸（转录 ≤1）；bot 先告知预计时长、完成主动推送；>2h 视频提示拆分 |
| QQ 消息长度/频率限制 | 中 | `/api/summary` 截断（默认 800 字）；bot 侧限频；完整内容可回"查看链接"（可选的 Web UI 只读入口） |
| 多用户并发 | 中 | 队列机制（3.3）防止资源争抢；按 task 隔离（现有结构天然支持） |
| DeepSeek key 落盘 | 中 | `.env` 权限 600、独立用户、禁 root |
| QQ 账号风控（bot 主动消息频率） | 中 | bot 框架侧限频（不在本项目范围，契约文档注明） |
| 磁盘增长 | 低 | 启动清理 tempvideo；cleanup_orphans 清理孤儿索引（P1.2） |

---

## 六、QQ bot 集成要点（交付给 hermes 侧）

1. **收到 B 站链接** → `POST /api/analyze`（带 token）→ 得到 `task_id` → 回复"开始解析《标题占位》…"（标题可在轮询到 meta 后更新）
2. **轮询** `/api/result/{task_id}`（2–5s，上限约 10 分钟；超时提示用户稍后查询）
3. **完成** → `GET /api/summary/{task_id}` → 发送文本；可选追问走 `/api/chat`
4. **失败/排队** → 直接转发 `message` 字段
5. 命令建议：`/最近`（history 前 5 条）、`/搜 <关键词>`（global chat）、`/删 <序号>`（batch_delete）——均可复用现有端点，属于 bot 侧工作

---

## 七、待确认清单（hermes 落地前必须回答）

1. 云厂商与地域（国内云 vs 海外云——决定 B 站 API 可达性与风控基线）？
2. CPU 核数 / 内存 / 磁盘？（决定 whisper 模型档位与并发数）
3. QQ bot 框架选型（NoneBot2 / OneBot-NapCat / 其他）？bot 与 scan 是否同机？
4. 是否已有公网域名/反代？Web UI 是否需要对 bot 用户开放（建议只开 API + 可选只读管理端）？
5. 单用户使用还是多用户？多用户是否需要按 QQ 号做限额？
