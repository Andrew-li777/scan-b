from src.llm import chat
from src.rag.vectorstore import index_subtitles, search, search_global

_CHAT_SYSTEM = """你是一个视频内容问答助手。根据提供的视频字幕片段回答用户问题。

规则:
1. 事实性内容必须来自字幕片段，不要编造
2. 用户可能在追问或使用指代（"它""第三种""刚才说的那个"）——请结合上文对话理解指代对象后再回答
3. 如果字幕中没有相关信息，明确说"视频中未提及"
4. 回答时引用具体的时间戳，方便用户定位
5. 保持回答简洁、准确
6. 用中文回答（除非字幕本身是英文）"""

_GLOBAL_SYSTEM = """基于以下所有视频的字幕片段回答问题。
如果引用了某个视频的内容，标注来源视频名称。
如果无法找到相关信息，请如实说不知道。"""


def build_index(task_id: str, texts: list[str], timestamps: list[float]):
    segments = [
        (f"{task_id}_{i}", text, ts)
        for i, (text, ts) in enumerate(zip(texts, timestamps))
    ]
    index_subtitles(task_id, segments)


def ask(task_id: str, question: str, history: list[dict] | None = None) -> str:
    results = search(task_id, question, k=5)
    if not results:
        return "视频中未提及相关内容。"

    context = "\n\n".join(
        f"[{_fmt_ts(r['timestamp'])}] {r['text']}" for r in results
    )
    prompt = f"字幕片段:\n{context}\n\n用户问题: {question}"
    # 带上多轮历史：前端一直在传 history，但旧代码收了不用 →
    # 追问"那第三点呢"这类指代全部失忆（README 宣称的多轮追问名不副实）
    return chat(_CHAT_SYSTEM, prompt, messages=_build_messages(history, question, prompt))


# 多轮上下文上限：最近 3 轮（6 条消息），单条截断 2000 字
_MAX_HISTORY_MSGS = 6
_MAX_HISTORY_CHARS = 2000


def _build_messages(history: list[dict] | None, question: str, prompt: str) -> list[dict]:
    """把前端传来的多轮历史拼成 messages。

    防御点：① 只认 user/assistant 两种角色（其余丢弃）；② 条数与单条长度上限；
    ③ 同角色相邻的合并（Anthropic 要求 user/assistant 交替）；④ 必须 user 开头；
    ⑤ 前端会把"当前问题"也塞在 history 末尾 → 去重，避免同一问题出现两次。
    """
    raw: list[tuple[str, str]] = []
    for item in (history or [])[-_MAX_HISTORY_MSGS:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        raw.append((role, content))

    if raw and raw[-1][0] == "user" and raw[-1][1] == question.strip():
        raw.pop()

    merged: list[dict] = []
    for role, content in raw:
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})

    if merged and merged[0]["role"] != "user":
        merged.pop(0)
    for m in merged:
        m["content"] = m["content"][:_MAX_HISTORY_CHARS]

    merged.append({"role": "user", "content": prompt})
    return merged


def global_ask(question: str) -> str:
    """Ask a question across all indexed videos."""
    results = search_global(question, k=5)
    if not results:
        return "所有视频中未找到相关内容。"
    context = "\n\n".join(
        f"[{r['source']} @ {_fmt_ts(r['timestamp'])}] {r['text']}"
        for r in results
    )
    # _GLOBAL_SYSTEM 只作为 system 传一次 —— 旧代码又拼进 prompt，
    # 等于每问一次都重复注入一份同样的指令，白烧 token
    prompt = f"字幕片段:\n{context}\n\n用户问题: {question}"
    return chat(_GLOBAL_SYSTEM, prompt)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
