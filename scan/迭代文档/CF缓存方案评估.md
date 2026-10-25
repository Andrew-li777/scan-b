# scan ③ 方案评估：CF 按角色分 Cache Key 缓存详情接口

> 评估日期 **2026-10-25**。性质：**只评估，不实施**（方案 ③ 在《优化升级方向》里一直标注「风险最高，须先看 ①② 叠加后的收益」）。
> 本文所有数字均为本机实测，测点与命令附在文末；未在本机验证的外部能力明确标「需确认」。

---

## 0. 一句话结论

**暂不做。** 当前瓶颈是「CF↔源站握手 0.9–1.1s + 出网 1Mbps」，而 ③ 能用边缘缓存省掉的只有前者的**一部分**；
同时它会把「行级可见性」这个应用层判断搬到 CDN，**一次配置错误就是跨角色泄漏**。建议先做 0 成本的
观测（重复访问率），重复率不足 30% 时收益无法覆盖风险。

---

## 1. 现状基线（2026-10-25 15:0x 实测）

| 指标 | 实测值 | 测点 |
|---|---|---|
| `/api/history` 走 CF（未压缩请求） | TTFB **1.302s** / 总 1.770s / 42,177 B | `curl -A <浏览器UA> https://scan.andrew-li.top/api/history` |
| 同上走 CF（gzip） | 4,960 B（−88%） | 10-23 记录，`Accept-Encoding: gzip` |
| 详情 `?subtitles=0`（长视频样本） | 15,383 B（原 64,650 B，−76%） | 问题记录 #75 |
| 重复切同一记录 | **304 零正文** | 问题记录 #75 |
| 源站直连 `:8787` | **15 ms** | 本机 curl |
| 源站 Caddy `:8080`（zstd） | `Content-Encoding: zstd` 生效 | 本机 curl + Host 头 |
| **边缘 HIT**（`/static/vendor/mermaid.min.js`，990,711 B gz） | TTFB **0.738s** / 总 2.791s（`cf-cache-status: HIT`，`age: 31`） | 同上，`Accept-Encoding: gzip` |
| CF↔源站 TLS 握手 | **0.9–1.1s**（波动 1.1–3.1s） | 问题记录 #75 的分层拆解 |

**读法**：把「动态 1.302s」和「边缘 HIT 0.738s」相减 ≈ **0.56s** —— 这就是 ③ 在这台机器上的**上限收益**
（本机到 CF 的 RTT ~200ms，所以 HIT 本身也不便宜；低 RTT 用户的实际差会更大，约 1.0–1.1s）。
**注意**：0.738s 这个数字含「本机→CF 的 TLS」，不是用户侧体验；用户侧边缘命中通常 50–150ms。

---

## 2. 四个方案对比

| # | 方案 | 改动 | 收益（本机口径） | 风险 | 前置条件 / 需确认 |
|---|---|---|---|---|---|
| **A** | **维持现状**（压缩 + ETag + 懒加载 + 会话 cookie） | 无 | 0.56s 拿不到，但已拿到 −76%~−96% 体积与 304 | 无 | — |
| **B** | **只缓存静态资源**（现状即是） | 无 | 已生效：HIT 0.738s / 全站 HTML 与 vendor 走缓存 | 低（静态、无身份） | 已确认 `cf-cache-status: HIT` |
| **C** | **缓存详情/列表**，Cache Key 含「角色」维度 | CF Cache Rule + 应用侧把角色暴露成可分桶的键（如按角色分路径 `/api/history?as=guest|user|admin`）+ 失效策略 | 重复访问 ~1.3s → ~0.7s（本机）/ 更低（用户侧） | **高**：分桶错=跨角色泄漏；删除/新增后缓存不失效=看到已删记录；`Vary` 处理不当=缓存污染 | **需确认**：CF 计划是否支持自定义 Cache Key（按 Header/Cookie 分桶）；免费版一般只能按 URL/Query 分桶 |
| **D** | **不动 CDN，改管道**：出网带宽 1Mbps→5Mbps（或迁腾讯云） | 花钱 / 迁移 | 正文传输 0.5s→0.1s；握手仍在 | 中（迁移另有 10 条风险清单，见《腾讯云迁移方案》） | 需报价与停机窗口 |

### 方案 C 的两个致命细节（为什么说「风险最高」）

1. **可见性判定在应用层**：`visible_for_role()` 按 `owner` 过滤记录；游客看到的列表 ≠ 常规用户 ≠ 管理员。
   CDN 只认 URL/Query/Header，**不认识 `owner`**。一旦两个角色落到同一个 cache key，就是把别人的记录发给游客。
2. **失效是分布式问题**：`/api/analyze` 新增记录、删除记录、批量删除、配额变更都要求缓存立刻失效；
   CF 的 purge 是按 URL/前缀/Tag 的，**按记录粒度 purge 会把同前缀的所有角色桶一起打掉**，等于缓存白做。

---

## 3. 建议路线（按成本从低到高）

1. **先观测，不花钱**（0 改动）：从 Caddy/uvicorn 访问日志统计「同一 `base_name` 的重复详情请求占比」。
   重复率 <30% → 缓存收益被稀释，**直接关闭 ③**，把这条记进《优化升级方向》的「已评估·关闭」。
2. **若重复率高**，先做 **C1 只缓存游客桶**（游客只看公开记录，身份单一、无泄漏面），
   用 `/api/history?scope=public` 这类显式 URL 分桶；登录态永远回源。风险从「高」降到「中」。
3. **仍不够再考虑 D**（带宽/迁移）——它同时解决上传下载与视频流，收益面比 ③ 大。

---

## 4. 决策清单（可勾选）

- [ ] **甲｜只做观测**：统计重复率，出结论后关闭或继续（0 成本、0 风险）— **推荐**
- [ ] 乙｜做 C1：仅缓存游客桶（需新增 `scope=public` 语义 + CF Cache Rule）
- [ ] 丙｜做完整 C：按角色分 Cache Key（需先确认 CF 计划能力，且要设计 purge 策略）
- [ ] 丁｜跳过 ③，直接评估 D（带宽/迁移）

---

## 5. 复现命令

```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
# 动态接口（走 CF）
curl -s -o /dev/null -A "$UA" -w 'ttfb=%{time_starttransfer}s total=%{time_total}s size=%{size_download}\n' \
  https://scan.andrew-li.top/api/history
# 边缘命中对照（静态、已缓存）
curl -s -D - -o /dev/null -A "$UA" -H 'Accept-Encoding: gzip' \
  https://scan.andrew-li.top/static/vendor/mermaid.min.js | grep -iE 'cf-cache-status|age:|content-encoding'
# 源站压缩（绕过 CF）
curl -s -D - -o /dev/null -H 'Accept-Encoding: zstd, gzip' -H 'Host: scan.andrew-li.top' \
  http://127.0.0.1:8080/api/history | grep -iE 'content-encoding'
```

> 说明：本机出网仅 ~1Mbps，`size_download` 大的样本（如 990KB 的 mermaid）总耗时会被本机上行拖长，
> **只看 TTFB**；用户侧体验不能直接用这台机器的 total 推断。
