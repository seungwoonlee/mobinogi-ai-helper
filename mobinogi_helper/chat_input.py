"""`@@` 채팅 입력 판정(03 §6.1·§6.2). 예외를 던지지 않고 값 객체를 돌려준다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import unicodedata

PREFIX = "@@"
MAX_CHAT_LENGTH = 50
EMOJI_SUFFIX_LENGTH = 2  # 공백 1자 + 단일 코드포인트 이모지 1자 [가정]
MAX_USER_LENGTH = MAX_CHAT_LENGTH - EMOJI_SUFFIX_LENGTH

# 이모지 결합에 쓰는 문자는 비가시문자여도 제거하지 않는다.
_KEEP_INVISIBLE = {"‍", "️"}

NOT_CHAT = "not_chat"
CHAT = "chat"
REJECTED = "rejected"

REASON_MULTILINE = "multiline"
REASON_EMPTY = "empty"
REASON_RESERVED = "reserved"
REASON_TOO_LONG = "too_long"


@dataclass(frozen=True)
class InputJudgment:
    kind: str
    message: Optional[str] = None
    reason: Optional[str] = None
    normalized: bool = False  # 정규화로 문구가 달라졌는지(미리보기에 정규화된 값을 보인다)
    length: int = 0

    @property
    def is_chat(self) -> bool:
        return self.kind == CHAT


def _strip_invisible(text: str) -> str:
    kept = []
    for char in text:
        if char in _KEEP_INVISIBLE:
            kept.append(char)
            continue
        if unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        kept.append(char)
    return "".join(kept)


def judge_input(line: str) -> InputJudgment:
    """한 줄 입력이 게임 채팅인지, 거부인지, 일반 대화인지 판정한다."""
    # 1. 개행은 정규화·제거보다 먼저 거부한다.
    if any(mark in line for mark in ("\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")):
        if _normalize(line).lstrip().startswith(PREFIX):
            return InputJudgment(REJECTED, reason=REASON_MULTILINE)
        return InputJudgment(NOT_CHAT)

    normalized = _normalize(line)
    stripped = normalized.lstrip()
    if not stripped.startswith(PREFIX):
        return InputJudgment(NOT_CHAT)

    body = stripped[len(PREFIX):].strip()
    changed = normalized.strip() != line.strip()
    if not body:
        return InputJudgment(REJECTED, reason=REASON_EMPTY, normalized=changed)
    if body.startswith(("/", "#")):
        return InputJudgment(REJECTED, reason=REASON_RESERVED, normalized=changed)
    if len(body) > MAX_USER_LENGTH:
        return InputJudgment(REJECTED, reason=REASON_TOO_LONG, normalized=changed, length=len(body))
    return InputJudgment(CHAT, message=body, normalized=changed, length=len(body))


def _normalize(text: str) -> str:
    return _strip_invisible(unicodedata.normalize("NFKC", text))


def parse_chat_input(line: str) -> Optional[str]:
    """기존 호출 호환 래퍼: 채팅이면 문구, 일반 입력이면 None, 거부면 ValueError."""
    judgment = judge_input(line)
    if judgment.kind == NOT_CHAT:
        return None
    if judgment.kind == REJECTED:
        raise ValueError(reject_message(judgment))
    return judgment.message


def reject_message(judgment: InputJudgment) -> str:
    if judgment.reason == REASON_MULTILINE:
        return "한 줄만 보낼 수 있어요."
    if judgment.reason == REASON_EMPTY:
        return "보낼 말을 입력해주세요."
    if judgment.reason == REASON_RESERVED:
        return "슬래시(/)나 #로 시작하는 말은 보낼 수 없어요."
    if judgment.reason == REASON_TOO_LONG:
        return (
            f"이모지가 붙어서 {MAX_USER_LENGTH}자까지 보낼 수 있어요. "
            f"지금은 {judgment.length}자예요."
        )
    return "보낼 수 없는 입력이에요."
