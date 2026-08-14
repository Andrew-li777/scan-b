"""Generate Mermaid mindmap from video analysis results using LLM."""

from src.llm import chat

_MINDMAP_SYSTEM = """你是一个知识结构化专家。根据视频内容生成 Mermaid mindmap 格式的思维导图。

规则:
1. 只输出 `mindmap` 类型的 Mermaid 代码（不要用 graph TD）
2. 根节点为视频核心主题
3. 二级节点为主要章节或核心概念（3-6个）
4. 三级节点为具体知识点或方法论（每个二级节点下 2-4 个）
5. 节点文本后可用括号标注关键时间戳，如 `梯度下降 (5:32)`
6. 节点文本不要过长，控制在 15 字以内
7. 不要输出任何解释文字，只输出 Mermaid 代码块

输出格式:
```mermaid
mindmap
  root((视频核心主题))
    概念A
      知识点A1 (5:32)
      知识点A2 (8:15)
    概念B
      知识点B1 (12:40)
      ...
```"""


def generate_mindmap(title: str, summary: str, keypoints: list[dict]) -> str:
    kp_text = "\n".join(
        f"- [{_fmt_ts(kp['timestamp'])}] {kp['content']}" for kp in keypoints[:15]
    )
    prompt = f"视频标题: {title}\n\n摘要:\n{summary[:2000]}\n\n重点:\n{kp_text}"
    result = chat(_MINDMAP_SYSTEM, prompt, max_tokens=2048)
    return _extract_mermaid(result)


def _extract_mermaid(text: str) -> str:
    if "```mermaid" in text:
        return text.split("```mermaid")[1].split("```")[0].strip()
    if "```" in text:
        return text.split("```")[1].split("```")[0].strip()
    return text.strip()


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"
