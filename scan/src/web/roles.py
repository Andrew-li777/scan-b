#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色与可见性规则（2026-10-15 引入常规用户角色）

三种角色
--------
| 角色 | 怎么来的 | 能干什么 |
|---|---|---|
| `guest` | 没带 Authorization 头 | 只读白名单（`guest_rules.guest_read_allowed`） |
| `user`  | 凭据有效且凭据文件里 `角色: user` | 游客全部 + 提交解析（日配额）/问答/下载导出/删自己的 |
| `admin` | 凭据有效且角色为 admin（**旧凭据文件缺省即 admin**） | 全部（含合集分P、批量导出/删除、对比） |

记录可见性（用户 2026-10-15 拍板）
---------------------------------
- 管理员解析的记录、以及**没有 owner 的存量记录** → `public`（游客可见）
- **常规用户解析的记录 → `members`（仅登录用户与管理员可见，游客不可见）**

`can_view_record()` 是唯一的判据，列表过滤、详情 404、导图 404 三处共用，
**不要**在路由里各写一份（这是"同一规则散落多处"的老坑）。
"""
from __future__ import annotations

GUEST = "guest"
USER = "user"
ADMIN = "admin"

ROLES = (GUEST, USER, ADMIN)

# 记录可见性取值
PUBLIC = "public"     # 任何人（含游客）
MEMBERS = "members"   # 仅登录用户 + 管理员


def normalize_role(role: str | None) -> str:
    """把凭据文件里的角色字符串规整为三种之一。

    无法识别的一律降级为 `user`（相对 admin 而言是 **fail closed**：给最低的登录权限，
    绝不给管理员）。
    """
    r = (role or "").strip().lower()
    if r in ("admin", "administrator", "管理员"):
        return ADMIN
    if r in ("user", "member", "常规", "常规用户"):
        return USER
    if r in ("guest", "游客", "visitor"):
        return GUEST
    return USER


def viewer(request) -> tuple[str, str]:
    """从请求上下文取 (账号, 角色)。中间件已经在 scope['state'] 里写好了。

    直接读 `request.scope`（而不是 `request.state`）是为了在不依赖 starlette
    版本的实现细节 —— 两者其实是同一份数据。
    """
    st = request.scope.get("state") or {}
    user = st.get("user", "") or ""
    role = st.get("role", "") or GUEST
    return user, role


# 常规用户**明确允许**的路径（管理员全放行，游客走 guest_read_allowed）
_USER_POST_PREFIXES = (
    "/api/analyze",        # 提交单链接解析（/api/analyze/playlist 在下面单独否决）
    "/api/chat",           # 单视频问答
    "/api/cancel/",        # 取消自己的任务（归属由路由判）
    # 批量删除：**允许**，但路由层只删得掉自己的记录（别人的进 failed 列表）——
    # 界面上的删除入口只有批量这一个，若此处禁掉常规用户就等于"不能删自己的"
    "/api/history/batch_delete",
    "/api/logout",         # 退出登录（删会话 cookie）
)
_USER_GET_PREFIXES = (
    "/api/chat/global",    # 全局问答
    "/api/progress/",      # 进度 SSE（归属由路由判）
    "/api/download/",      # 下载（可见性由路由判）
    "/api/export/",        # 单条导出（/api/export/batch 在下面单独否决）
)

# 常规用户**明确否决**的路径（前缀匹配已放行的要在这里挑出来）
_USER_DENY = (
    "/api/analyze/playlist",   # 合集/分P 解析 —— 仅管理员
    "/api/export/batch",       # 批量导出 —— 仅管理员
    "/api/compare",            # 对比 —— 仅管理员
)


def member_allowed(method: str, path: str, is_read_allowed) -> bool:
    """登录用户（role=user）能否访问 (method, path)。

    行为：
    - 游客白名单里的 GET 一律允许（读的能力是叠加的）
    - 再叠加"用户级"写/读路径，但先扣掉 `_USER_DENY`
    """
    p = path.split("?", 1)[0]

    for d in _USER_DENY:
        if p.startswith(d):
            return False

    if method in ("GET", "HEAD", "OPTIONS"):
        if is_read_allowed(method, p):
            return True
        return any(p.startswith(x) for x in _USER_GET_PREFIXES)

    if method == "POST":
        return any(p.startswith(x) for x in _USER_POST_PREFIXES)
    if method == "DELETE":
        # 删除单条记录（是否"自己的"由路由判）
        return p.startswith("/api/history/")
    if method in ("PUT", "PATCH"):
        return False
    return False


def can_view_record(role: str, rec: dict) -> bool:
    """某角色能否查看这条记录（列表过滤 / 详情 404 / 导图 404 共用）。

    `rec` 是记录元信息 dict（详情结构或 meta.json 均可）。
    """
    if role == ADMIN:
        return True

    vis = (rec.get("visibility") or "").strip().lower()
    if not vis:
        # 没有显式 visibility → 由归属推导；无 owner 的一律视为 public（存量 36 条）
        owner = (rec.get("owner") or "").strip()
        owner_role = (rec.get("owner_role") or "").strip().lower()
        vis = MEMBERS if (owner and owner_role == USER) else PUBLIC

    if vis == PUBLIC:
        return True
    return role == USER
