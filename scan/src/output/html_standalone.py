"""Self-contained HTML page generation from video analysis results."""

from pathlib import Path

from src.web.history import _load_detail


def _esc(s: str) -> str:
    """Minimal HTML escape."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_CSS = """
:root{--bg:#f5f6fa;--card:#fff;--border:#dcdde1;--text:#2d3436;--muted:#636e72;--accent:#00b894;--radius:8px;--danger:#c0392b}
@media (prefers-color-scheme:dark){
:root{--bg:#0f0f0f;--card:#1a1a1a;--border:#2a2a2a;--text:#e0e0e0;--muted:#888;--accent:#00b894;--danger:#e74c3c}
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei','PingFang SC','Hiragino Sans GB','Noto Sans CJK SC','Source Han Sans SC',sans-serif;background:var(--bg);color:var(--text);padding:24px;line-height:1.6}
h1{font-size:1.4rem;color:var(--accent);margin-bottom:4px}
.hero{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:20px;display:flex;gap:16px;align-items:flex-start}
.hero img{width:160px;height:90px;object-fit:cover;border-radius:4px;background:var(--border);flex-shrink:0}
.hero .meta p{color:var(--muted);font-size:0.85rem;margin-bottom:2px}
.section{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.section h2{font-size:1.1rem;margin-bottom:12px;color:var(--accent);padding-bottom:8px;border-bottom:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:0.9rem}
th{text-align:left;padding:8px;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600}
td{padding:8px;border-bottom:1px solid var(--bg);vertical-align:top}
.stars{color:#f1c40f;white-space:nowrap}
.time{color:var(--accent);text-decoration:none;white-space:nowrap}
.sub-line{padding:3px 0;font-size:0.9rem;color:var(--muted);border-bottom:1px solid var(--bg)}
.overview-text{white-space:pre-wrap;line-height:1.7;font-size:0.9rem}
.footer{text-align:center;padding:16px 0;color:var(--muted);font-size:0.8rem}

/* PDF / print rules */
@page{size:A4;margin:1.8cm 1.5cm;@bottom-center{content:counter(page) " / " counter(pages);font-size:9pt;color:#888}}
@media print{
  body{background:#fff !important;color:#000 !important;padding:0}
  .hero,.section{background:#fff !important;border:1px solid #ccc !important;page-break-inside:avoid;box-shadow:none}
  .hero{page-break-after:avoid}
  table{page-break-inside:auto}
  tr{page-break-inside:avoid;page-break-after:auto}
  thead{display:table-header-group}
  h1,h2,h3{page-break-after:avoid;color:#000 !important}
  .section h2{border-bottom-color:#888 !important}
  th{color:#000 !important;border-bottom-color:#888 !important}
  .time{color:#000 !important}
  .sub-line{page-break-inside:avoid;color:#222 !important;border-bottom:none}
  .footer{display:none}
}
"""


def generate_html_from_history(base_name: str) -> str | None:
    """Generate a self-contained HTML page from a history record.

    Returns the full HTML string with inline CSS, or None if the record doesn't exist.
    """
    detail = _load_detail(base_name)
    if detail is None:
        return None

    title = detail.get("title", base_name)
    thumbnail = detail.get("thumbnail", "")
    platform = detail.get("platform", "")
    author = detail.get("author", "")
    duration = detail.get("duration", "")
    url = detail.get("url", "")
    overview = detail.get("overview", "")
    keypoints = detail.get("keypoints", [])
    subtitles = detail.get("subtitles", [])

    meta_parts = [p for p in [platform, author, duration] if p]
    meta_line = " | ".join(meta_parts)
    url_html = f'<a href="{_esc(url)}" target="_blank" style="color:var(--accent)">原链</a>' if url else ""

    # Parse summary from overview frontmatter
    summary = ""
    if overview:
        parts = overview.split("---")
        if len(parts) >= 2:
            summary = parts[1].strip()[:500]

    # Keypoints table
    kp_html = ""
    if keypoints:
        rows = []
        for kp in keypoints:
            stars = "★" * kp["importance"] + "☆" * (5 - kp["importance"])
            ts = _fmt_ts(kp["timestamp"])
            content = _esc(kp["content"])
            rows.append(f"<tr><td class=\"time\">[{ts}]</td><td>{content}</td><td class=\"stars\">{stars}</td></tr>")
        kp_html = """
        <div class="section">
        <h2>重点标示</h2>
        <table><thead><tr><th>时间</th><th>内容</th><th>重要度</th></tr></thead>
        <tbody>{rows}</tbody></table>
        </div>
        """.format(rows="\n".join(rows))

    # Subtitles
    sub_html = ""
    if subtitles:
        lines = "\n".join(
            f'<div class="sub-line">{_esc(s)}</div>' for s in subtitles
        )
        sub_html = f"""
        <div class="section">
        <h2>完整字幕</h2>
        {lines}
        </div>
        """

    # Overview
    overview_html = ""
    if overview:
        overview_html = f"""
        <div class="section">
        <h2>内容概览</h2>
        <div class="overview-text">{_esc(overview)}</div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} - Video Analyzer</title>
<style>{_CSS}</style>
</head>
<body>

<div class="hero">
<img src="{_esc(thumbnail)}" alt="" onerror="this.style.display='none'">
<div class="meta">
<h1>{_esc(title)}</h1>
<p>{_esc(meta_line)}</p>
<p>{url_html}</p>
</div>
</div>

{kp_html}
{sub_html}
{overview_html}

<div class="footer">Generated by Video Analyzer</div>
</body>
</html>"""


def _fmt_ts(seconds: float | int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"