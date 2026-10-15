#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体 URL 小工具。

背景（2026-10 真机踩坑）：B 站封面接口 /x/web-interface/view 返回的 `pic`
字段是 **http://iX.hdslb.com/...**。而生产环境 scan.andrew-li.top 由 Caddy
下发 CSP：

    img-src 'self' data: https://*.hdslb.com https://*.ytimg.com

CSP 的 scheme-source 是**严格 scheme 匹配** —— `https://*.hdslb.com` 不匹配
`http://` 开头的 URL，于是封面被浏览器直接拒载（DevTools 报 "Refused to load
the image ... violates CSP directive img-src"）。同时 http 图在 https 页属
mixed content，Firefox 拦、Chrome 自动升级，表现随浏览器而异。

修法：在「入库」和「出库」两端统一把 http 提升为 https（B 站图床 https 可用，
实测 20/20 张 206）。放在这里集中定义，避免散落多处正则。
"""


def https_url(u: str | None) -> str:
    """把 http:// 的媒体 URL 提升为 https://；其余原样返回。

    - 空值 → ""
    - `http://host/path` → `https://host/path`
    - `//host/path`（协议相对）→ `https://host/path`
    - 已是 https / data: / 相对路径 → 原样
    """
    if not u:
        return ""
    u = str(u).strip()
    low = u.lower()
    if low.startswith("http://"):
        return "https://" + u[7:]
    if u.startswith("//"):
        return "https:" + u
    return u
