import json

from src.llm import chat_json

_CLASSIFY_SYSTEM = """根据视频标题和摘要，判断视频的分类并提取关键词标签。

分类选项（选1-2个）：科技/AI/编程/教程/VPS/网络/产品/设计/商业/教育/娱乐/生活/游戏/知识科普/其他
额外提取3-5个关键词作为标签。

输出JSON格式：{"category": "科技", "tags": ["VPS", "翻墙", "CN2"]}"""


def classify(title: str, summary: str) -> dict:
    prompt = f"标题: {title}\n\n摘要开头: {summary[:800]}"
    try:
        result = chat_json(_CLASSIFY_SYSTEM, prompt, max_tokens=512)
        category = str(result.get("category", "其他"))
        tags = [str(t) for t in result.get("tags", [])][:5]
        return {"category": category, "tags": tags}
    except Exception:
        return {"category": "其他", "tags": []}
