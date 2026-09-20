"""write_chat 응답 판정표(03 §6.6). 표에 없는 값은 모두 불명이다."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
from typing import Optional

from .cli_adapter import RawResult
from .reasons import Failure, Reason
from .response_validator import find_marker, parse_object

ENV_KNOWN = "MOBINOGI_KNOWN_RESPONSES"
DEFAULT_KNOWN_PATH = Path(__file__).resolve().parent.parent / "config" / "known_responses.json"

_NOT_SUCCESS_STATUSES = {"blocked", "timeout", "canceled"}


class VerdictKind(str, Enum):
    SENT = "sent"          # 전송 확정
    NOT_SENT = "not_sent"  # 미전송 확정
    UNKNOWN = "unknown"    # 불명


@dataclass(frozen=True)
class KnownResponses:
    """실측으로 확정한 응답 목록. 기본값은 비어 있으며 비어 있으면 모든 응답이 불명이다."""

    config_verified: bool = False
    sent_statuses: frozenset = frozenset()
    rejected_statuses: frozenset = frozenset()

    @property
    def usable(self) -> bool:
        return self.config_verified and bool(self.sent_statuses or self.rejected_statuses)


@dataclass(frozen=True)
class SendVerdict:
    kind: VerdictKind
    reason: Reason = Reason.NONE
    status: Optional[str] = None
    retry_after: Optional[float] = None


def load_known_responses(path: Optional[Path] = None) -> tuple[KnownResponses, Optional[str]]:
    """설정을 읽는다. 없거나 손상됐거나 형식이 틀리면 빈 설정과 경고를 돌려준다(fail-closed)."""
    target = Path(path) if path else Path(os.environ.get(ENV_KNOWN, DEFAULT_KNOWN_PATH))
    data = None
    problem = None
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        problem = "알려진 응답 설정 파일이 없어요."
    except (OSError, ValueError):
        problem = "알려진 응답 설정 파일을 읽을 수 없어요."
    if problem:
        return KnownResponses(), problem

    if not isinstance(data, dict) or data.get("config_verified") is not True and data.get("config_verified") is not False:
        return KnownResponses(), "알려진 응답 설정의 형식이 올바르지 않아요."
    sent = data.get("sent_statuses", [])
    rejected = data.get("rejected_statuses", [])
    if not _is_string_list(sent) or not _is_string_list(rejected):
        return KnownResponses(), "알려진 응답 설정의 형식이 올바르지 않아요."
    return (
        KnownResponses(
            config_verified=data["config_verified"],
            sent_statuses=frozenset(sent),
            rejected_statuses=frozenset(rejected),
        ),
        None,
    )


def _is_string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


_FAILURE_REASONS = {
    Failure.TIMEOUT: Reason.TIMEOUT,
    Failure.LAUNCH_FAILED: Reason.LAUNCH_FAILED,
    Failure.OUTPUT_TOO_LARGE: Reason.OUTPUT_TOO_LARGE,
    Failure.INTERRUPTED: Reason.USER_INTERRUPT,
}


def judge_write_chat(raw: RawResult, known: KnownResponses) -> SendVerdict:
    """종료 코드·응답 본문으로 전송 확정 / 미전송 확정 / 불명을 판정한다."""
    if raw.failure is not None:
        return SendVerdict(VerdictKind.UNKNOWN, _FAILURE_REASONS[raw.failure])

    parsed = parse_object(raw)
    if not parsed.ok:
        if raw.exit_code == 5:
            return SendVerdict(VerdictKind.UNKNOWN, Reason.CODE_5)
        if raw.exit_code not in (0, None):
            return SendVerdict(VerdictKind.UNKNOWN, Reason.PROCESS_EXIT)
        return SendVerdict(VerdictKind.UNKNOWN, parsed.reason)
    data = parsed.data

    status = data.get("status")
    if not isinstance(status, str):
        return SendVerdict(VerdictKind.UNKNOWN, Reason.NOT_STRING_STATUS)

    retry_after = find_marker(data, "retryAfterSeconds")
    retry_present = retry_after is not None
    retry_value = float(retry_after) if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) else None

    if known.config_verified:
        if status in known.rejected_statuses:
            return SendVerdict(VerdictKind.NOT_SENT, Reason.REJECTED, status, retry_value)

    has_marker = (
        find_marker(data, "error") is not None
        or retry_present
        or status in _NOT_SUCCESS_STATUSES
    )
    if known.config_verified and status in known.sent_statuses and raw.exit_code == 0 and not has_marker:
        return SendVerdict(VerdictKind.SENT, Reason.NONE, status)

    if has_marker:
        reason = Reason.NOT_SUCCESS_MARKER
    elif raw.exit_code == 5:
        reason = Reason.CODE_5
    elif not known.usable:
        reason = Reason.NO_KNOWN_RESPONSES
    elif raw.exit_code != 0:
        reason = Reason.PROCESS_EXIT
    else:
        reason = Reason.UNKNOWN_STATUS
    return SendVerdict(VerdictKind.UNKNOWN, reason, status, retry_value)
