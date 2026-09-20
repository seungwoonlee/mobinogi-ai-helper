"""채팅에 붙는 이모지와 행동 규칙. 행동은 뚜렷한 표현일 때만 붙인다(03 §10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_RULES = (
    (("미안", "죄송", "사과"), "😓", "/사과1"),
    (("축하", "ㅊㅋ"), "🥳", "/축하해"),
    (("고마", "감사"), "😍", "/하트"),
    (("사랑", "좋아해"), "😍", "/하트"),
    (("안녕", "반가", "어서"), "😊", "/손인사1"),
    (("잘 가", "잘가", "수고", "이만 갈", "이만갈"), "😉", "/손인사1"),
    (("화이팅", "힘내", "응원"), "🥳", "/응원댄스"),
    (("ㅋㅋ", "ㅎㅎ", "웃기", "재밌"), "🤣", "/웃기1"),
    (("슬프", "아쉽", "흑흑"), "😢", "/울기1"),
    (("최고", "대박", "짱", "신난"), "😎", "/최고"),
)

# 앞에 붙으면 뜻이 뒤집히는 표현
_NEGATION_PREFIXES = ("안 ", "안", "못 ", "못")
# 키워드가 다른 뜻으로 쓰인 표현(예: 물건 이름)
_EXCLUSIONS = {
    "사과": ("사과 팔", "사과팔", "사과나무", "사과 나무", "사과 파", "사과파이", "사과 주스", "사과주스", "사과 판", "사과판"),
}

DEFAULT_EMOJI = "😊"


def behaviour_names() -> frozenset[str]:
    """자동 규칙이 붙일 수 있는 행동 이름(허용 행동 목록의 내장 값)."""
    return frozenset(behaviour for _, _, behaviour in _RULES)


@dataclass(frozen=True)
class ChatPlan:
    """전송할 대사와, 의도가 뚜렷할 때만 실행할 행동."""

    text: str
    behaviour: Optional[str]


def _keyword_applies(message: str, keyword: str) -> bool:
    start = 0
    while True:
        index = message.find(keyword, start)
        if index < 0:
            return False
        start = index + len(keyword)
        before = message[:index]
        if any(before.endswith(prefix) for prefix in _NEGATION_PREFIXES):
            continue
        if any(message.startswith(excluded, index) for excluded in _EXCLUSIONS.get(keyword, ())):
            continue
        return True


def build_chat_plan(message: str, auto_behaviour: bool = True) -> ChatPlan:
    """문장 의도에 맞춰 이모지를 붙이고 행동을 고른다.

    모든 대사에는 이모지를 붙인다. 행동은 뜻이 분명한 경우에만 붙이며,
    `auto_behaviour=False`이면 붙이지 않는다.
    """
    lowered = message.lower()
    for keywords, emoji, behaviour in _RULES:
        if any(_keyword_applies(lowered, keyword) for keyword in keywords):
            return ChatPlan(f"{message} {emoji}", behaviour if auto_behaviour else None)
    return ChatPlan(f"{message} {DEFAULT_EMOJI}", None)
