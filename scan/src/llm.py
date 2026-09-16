import json

from anthropic import Anthropic

from src.config import settings

_client = Anthropic(**settings.anthropic_kwargs)


def _has_text_block(content) -> bool:
    """True if the response contains a real text block."""
    for block in content or []:
        if getattr(block, "type", None) == "text":
            return True
    return False


def _extract_text(content) -> str:
    """Extract the text block from an Anthropic response content list.

    DeepSeek's Anthropic-compatible endpoint returns a ThinkingBlock
    (type="thinking") as the first content item; the real text is in the
    TextBlock (type="text") after it. Prefer the text block. The thinking
    content is NEVER returned as the answer — it is model-internal
    reasoning and is not suitable output.
    """
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text or ""
    return ""


def chat(
    system_prompt: str,
    user_message: str = "",
    *,
    model: str | None = None,
    max_tokens: int = 4096,
    messages: list[dict] | None = None,
    _retries: int = 0,
) -> str:
    """Chat with the LLM.

    If the model consumes the whole token budget on its thinking block and
    returns no text block, retry once with a larger budget before giving up
    (thinking text is model-internal and must not leak into output).
    """
    msgs = messages or [{"role": "user", "content": user_message}]
    r = _client.messages.create(
        model=model or settings.anthropic_model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=msgs,
    )
    text = _extract_text(r.content)
    if not text and _retries < 1:
        # No text block (thinking likely ate the whole budget) — retry bigger.
        return chat(
            system_prompt,
            messages=msgs,
            model=model,
            max_tokens=int(max_tokens * 2),
            _retries=_retries + 1,
        )
    return text


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
