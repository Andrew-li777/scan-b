import json

from anthropic import Anthropic

from src.config import settings

_client = Anthropic(**settings.anthropic_kwargs)


def chat(
    system_prompt: str,
    user_message: str = "",
    *,
    model: str | None = None,
    max_tokens: int = 4096,
    messages: list[dict] | None = None,
) -> str:
    msgs = messages or [{"role": "user", "content": user_message}]
    r = _client.messages.create(
        model=model or settings.anthropic_model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=msgs,
    )
    # Handle DeepSeek's ThinkingBlock (type="thinking") — find the text block
    for block in r.content:
        if getattr(block, "type", None) == "text":
            return block.text
    # Fallback for models that don't return thinking blocks
    return r.content[0].text


def chat_json(system_prompt: str, user_message: str, *, model: str | None = None, max_tokens: int = 4096) -> dict:
    combined = system_prompt + "\n\n请严格按照 JSON 格式输出，不要输出其他内容。"
    text = chat(combined, user_message, model=model or settings.anthropic_reasoning_model, max_tokens=max_tokens)
    text = text.strip()
    # Strip possible markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text)
