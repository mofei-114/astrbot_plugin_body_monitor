"""Policy for deciding when health data may enter an LLM request."""

from collections.abc import Iterable
from typing import Any


_HEALTH_TERMS = (
    "身体",
    "健康",
    "心率",
    "血氧",
    "睡眠",
    "步数",
    "体重",
    "体脂",
    "bmi",
    "压力",
    "健康数据",
    "身体数据",
    "监测数据",
)
_QUERY_TERMS = (
    "查看",
    "查询",
    "多少",
    "怎么样",
    "如何",
    "状态",
    "数据",
    "记录",
    "趋势",
    "基线",
    "告警",
    "异常",
)


def should_inject_health_data(
    event: Any, configured_targets: Iterable[str], prompt: str | None = None
) -> bool:
    if bool(getattr(event, "private_companion_proactive_framework", False)):
        return False
    if bool(getattr(event, "_private_companion_external_proactive_source", False)):
        return False

    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if not origin or origin not in set(configured_targets):
        return False
    if _is_group_event(event, origin):
        return False

    message = str(getattr(event, "message_str", "") or prompt or "").strip()
    lowered = message.lower()
    if lowered.startswith("/body_"):
        return True
    return any(term in lowered for term in _HEALTH_TERMS) and any(
        term in lowered for term in _QUERY_TERMS
    )


def _is_group_event(event: Any, origin: str) -> bool:
    get_group_id = getattr(event, "get_group_id", None)
    if callable(get_group_id):
        try:
            if get_group_id():
                return True
        except Exception:
            return True
    elif getattr(event, "group_id", None):
        return True
    lowered_origin = origin.lower()
    return "groupmessage" in lowered_origin or "group" in lowered_origin
