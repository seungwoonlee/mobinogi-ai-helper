"""전송 출처 규칙(03 §5.4). origin은 호출자의 주장이 아니라 검사를 통과한 값이다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Callable, Optional

from .command_catalog import Kind


class Provenance(str, Enum):
    TYPED = "typed"          # 키 입력으로 전부 새로 작성
    PASTED = "pasted"        # 붙여넣기·음성·끌어다 놓기·복원
    PREFILLED = "prefilled"  # AI가 채웠거나 그것을 수정한 문구


class Origin(str, Enum):
    USER_AT = "USER_AT"
    AI_APPROVED = "AI_APPROVED"
    SYSTEM = "SYSTEM"
    AI_PREFILL = "AI_PREFILL"  # 전송 불가 표시용


@dataclass(frozen=True)
class Approval:
    """승인 카드에서 `실행`을 눌러 발급되는 승인. action_id 하나에만 유효하다."""

    action_id: str
    expires_at: float  # 단조 시계 기준


@dataclass(frozen=True)
class SendDecision:
    allowed: bool
    reason: str = ""


def check_send_allowed(
    origin: Optional[Origin],
    provenance: Provenance,
    kind: Kind,
    *,
    paste_confirmed: bool = False,
    approval: Optional[Approval] = None,
    action_id: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> SendDecision:
    if origin is None:
        return SendDecision(False, "origin이 없어요")
    if origin == Origin.AI_PREFILL:
        return SendDecision(False, "AI가 채운 문구는 승인 카드를 거쳐야 해요")
    if origin == Origin.SYSTEM:
        if kind == Kind.QUERY:
            return SendDecision(True)
        return SendDecision(False, "SYSTEM 출처는 조회 전용이에요")
    if origin == Origin.AI_APPROVED:
        if approval is None or approval.action_id != action_id:
            return SendDecision(False, "이 작업에 대한 승인이 없어요")
        if clock() > approval.expires_at:
            return SendDecision(False, "승인 유효 시간이 지났어요")
        return SendDecision(True)
    if origin == Origin.USER_AT:
        if provenance == Provenance.TYPED:
            return SendDecision(True)
        if provenance == Provenance.PASTED and paste_confirmed:
            return SendDecision(True)
        if provenance == Provenance.PASTED:
            return SendDecision(False, "붙여넣은 문구는 확인이 필요해요")
        return SendDecision(False, "AI가 채운 문구는 직접 입력한 문구로 인정되지 않아요")
    return SendDecision(False, "알 수 없는 출처예요")
