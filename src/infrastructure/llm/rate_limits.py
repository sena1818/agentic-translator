"""
限流与额度错误分类

不同服务商的 429 含义不同：每分钟限流应等待后重试；每日额度用完则重试无意义，
应换备用模型或停止并等待断点续跑。这里只做基于错误文本的确定性分类。
"""

from __future__ import annotations

import re

_DURATION_RE = re.compile(r"retry in\s+((?:\d+(?:\.\d+)?[hms])+)", re.IGNORECASE)
_DAILY_HINTS = ("per day", "perday", "requestsperday", "daily")


class QuotaExhaustedError(RuntimeError):
    """所有可用模型的每日额度都已用完；已完成的块已缓存，次日或换密钥后可断点续跑。"""


def _status(exc: BaseException) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def parse_retry_seconds(message: str) -> float | None:
    """解析 "Please retry in 1h2m3.5s" 这类提示，返回秒数。"""
    match = _DURATION_RE.search(message)
    if not match:
        return None
    total = 0.0
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", match.group(1)):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def _details(exc: BaseException) -> str:
    """异常文本 + 原始响应体。

    Gemini 的 OpenAI 兼容端点把错误包在 JSON 数组里，OpenAI SDK 解析不出，
    异常文本只剩 "Error code: 429"；额度类型与重试时间只在原始响应体中。
    """
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    try:
        if response is not None and getattr(response, "text", None):
            parts.append(response.text)
    except Exception:  # 流式响应未读取时访问 text 可能抛错
        pass
    body = getattr(exc, "body", None)
    if body:
        parts.append(str(body))
    return "\n".join(parts)


def classify(exc: BaseException) -> tuple[str | None, float | None]:
    """返回 (类别, 建议等待秒数)。类别：daily（每日额度用完）| rate（短期限流）| None。"""
    message = _details(exc)
    status = _status(exc)
    if status != 429 and "RESOURCE_EXHAUSTED" not in message and "429" not in message[:40]:
        return None, None
    lowered = message.lower().replace(" ", "")
    retry = parse_retry_seconds(message)
    if any(hint.replace(" ", "") in lowered for hint in _DAILY_HINTS) or (retry is not None and retry > 600):
        return "daily", retry
    return "rate", retry
