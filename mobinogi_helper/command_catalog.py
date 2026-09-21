"""명령 분류 표, 허용 행동 목록, requiresConfirm 정규화(03 §2, 02 §9)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from .chat_plan import behaviour_names


class Kind(str, Enum):
    QUERY = "QUERY"
    CHAT = "CHAT"
    BEHAVIOUR = "BEHAVIOUR"
    ACTION = "ACTION"


# 내장 분류 표. 목록에 없는 명령은 ACTION으로 취급해 승인을 요구한다.
_KNOWN = {
    "status": Kind.QUERY,
    "capabilities": Kind.QUERY,
    "get_my_info": Kind.QUERY,
    "get_activity": Kind.QUERY,
    "write_chat": Kind.CHAT,
}


def classify_command(command: str) -> Kind:
    return _KNOWN.get(command, Kind.ACTION)


def is_allowed_behaviour(text: str) -> bool:
    """`/행동` 문자열이 허용 행동 목록에 있는지."""
    return text in behaviour_names()


def requires_confirm(value: Any) -> bool:
    """requiresConfirm 값을 정규화한다. 누락·미지 값은 확인 필요(True)다."""
    if value is False:
        return False
    if isinstance(value, str) and value.strip().lower() == "false":
        return False
    return True
