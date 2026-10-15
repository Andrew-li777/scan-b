# scripts/verify — 回归验证脚本

`2026-10-10` 外部代码审查整改（见 [`迭代文档/问题记录.md`](../../迭代文档/问题记录.md) 第 61–68 条）时配套写下的验证脚本。
每一项修复都有对应的、**可复跑**的验证，避免「修了但没测」或「后人改坏了不知道」。

## 快速开始

```bash
# 离线组（不需要服务，秒级；建议每次改动后先跑这个）
./run_all.sh

# 全部（含需要服务的用例；需服务在跑 + 凭据就位）
./run_all.sh --all

# 额外开启慢速端到端（真实解析视频，可能数分钟）
./run_all.sh --all --e2e
```

解释器优先用仓库 venv；也可显式指定：

```bash
SCAN_PYTHON=/path/to/venv/bin/python ./run_all.sh
```

退出码：`0` = 全部通过（含 SKIP）；`1` = 有失败项。

## 用例清单

| 脚本 | 对应问题 | 验证内容 | 需要服务 |
|---|---|---|:--:|
| `verify_b2.py` | #61 B2 | 删记录同步清索引：`delete_index` 机制、派生键合规、`_purge_index` 全路径、库状态 | — |
| `verify_a6.py` | #62 A6 | 转录闸串行性、排队阻塞、取消响应、许可无泄漏 | — |
| `verify_a1.py` | #63 A1 | 临时目录清理生效、`_TEMP_VIDEO_DIR` 死引用已消除、幂等 | — |
| `verify_a3a7.py` | #67 A3+A7 | PATH 无污染、`generate_all` 功能等价、`global_ask` 不重复注入 system | — |
| `verify_a5.py` | #66 A5 | cookie 文件改动后热生效、60s 节流、还原一致 | — |
| `verify_b3.py` | #67 B3 | cache 命中即回放 done、旧格式不误命中、键清理 | — |
| `verify_a4.py` | #64 A4 | 同 URL 并发提交 → `[200, 409]`、SSE 可读 | ✅ |
| `verify_b1.py` | #65 B1 | `_build_messages` 单元行为 + 真实两轮指代问答 | ✅ |
| `verify_b1b.py` | #65 B1 | **决定性**：暗号对照（无历史→答不出，有历史→复述） | ✅ |
| `verify_b4.py` | #67 B4 | 路径穿越（明文/编码）→ 404、正常文件 200、`--with-e2e` 缓存 e2e | ✅ |
| `verify_thumb.py` | #69 封面/CSP | `https_url` 边界、前端 setThumb 提升 + `no-referrer`、Caddy CSP 白名单、详情接口 thumbnail 无 http | ✅ |
| `verify_roles.py` | #70 角色/权限 | 凭据多账号解析（旧格式兼容）、`member_allowed` / `can_view_record` 矩阵、前端角色门控、三角色 × 路由 API 实测 | ✅ |
| `verify_visibility.py` | #71 行级可见性 | 造临时记录（指定 owner）穿 HTTP 层打三角色矩阵：列表过滤 / 详情·导图 404 / 下载 / 导出 / 删除归属 / `meta_for_path` 向上查找 | ✅ |
| `verify_quota.py` | #72 配额与闸 | 配额账本单元（在飞上限 / 产出计数 / 失败返还 / 用尽文案 / 落盘持久化 / 管理员不限）+ 静态接线 + 线上 whoami quota 字段 | ✅ |

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCAN_BASE_URL` | `http://127.0.0.1:8787` | 服务地址 |
| `SCAN_CRED_FILE` | `/root/.scan-web-cred` | 凭据文件（仅读「账号/密码」行）。**默认取 `角色: admin` 的账号** |
| `SCAN_CRED_ACCOUNT` | （空） | 指定用哪个账号跑用例（多角色用例用它取常规用户） |
| `SCAN_TEST_URL` | 某个 B站视频 | 端到端解析用的测试视频 |
| `SCAN_PYTHON` | 仓库 venv | 运行脚本用的解释器 |

**凭据永不入库**：脚本从 `SCAN_CRED_FILE` 读取，只打印「已就绪 / 缺失」。缺凭据或服务未起时，
需要服务的用例会打印 `[SKIP]` 并以退出码 0 结束（不阻塞离线组）。

## 注意事项

1. **会改动少量真实状态**（均在测试内还原）：
   - `verify_b2.py` 会在向量库中建/删一个假集合 `BV1TEST0001`（不碰真实记录）；
   - `verify_a5.py` 会临时追加一行假 cookie 到 `bilibili_cookies.txt`，`finally` 中还原；
   - `verify_a1.py` 会真的清理 `tempvideo/` 下的空目录（这正是修复目标行为）。
2. **`verify_b4.py --with-e2e` 很慢**（首次解析需下载+转录，可能数分钟），默认关闭。
3. 断言依赖少量**样本数据**（历史记录目录里的 `[P9]…` 记录、任一 `meta.json`）。
   找不到样本时相关断言会跳过而非失败 —— 换机器/清空记录后仍可跑。
4. 脚本通过 `_common.py` 显式加载仓库根 `.env`（`src/config.py` 的 `env_file` 是相对 CWD 的，
   否则 `output_dir` 会退回默认值，导致脚本看到的记录目录与服务实际使用的不一致）。

## 新增用例

照抄现有脚本结构即可：

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk, basic_auth, service_reachable, skip, http, BASE

chk("断言名", 条件, 失败时附带的详情)
sys.exit(chk.finish("项X 描述"))
```

`_common.py` 还提供 `find_page_dir()` / `sample_record_rel()` / `record_folders()` 等样本发现工具，
请**不要硬编码具体记录名或绝对路径**。
