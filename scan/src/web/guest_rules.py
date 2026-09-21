#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan (video analyzer) 游客读权限判定

游客可读：首页、历史列表、历史详情、思维导图
游客不可读：download、export（html+pdf）、progress SSE、chat/global、一切写操作
"""


def guest_read_allowed(method: str, path: str) -> bool:
    """游客能否访问 (method, path)。仅 GET/HEAD 会走到这里。"""
    p = path.split("?", 1)[0]

    # 下载/导出/进度/LLM 搜索：严格拒绝（耗资源或含内部文件）
    if p.startswith(("/api/download/", "/api/export/", "/api/progress/", "/api/chat/")):
        return False

    # 历史列表与详情、思维导图
    if p.startswith("/api/history"):
        return True
    if p.startswith("/api/mindmap/"):
        return True

    # 首页
    if p in ("/", "/favicon.ico"):
        return True

    # 自托管静态资源（vendor/mermaid.min.js 等前端库，无敏感内容）
    if p.startswith("/static/"):
        return True

    return False
