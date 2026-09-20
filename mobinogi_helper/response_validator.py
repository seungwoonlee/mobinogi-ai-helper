"""명령별 응답 계약 검증. 계약 위반만 분류하고 성공을 추측하지 않는다(02 §6.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Optional

from .cli_adapter import RawResult
from .reasons import Reason


@dataclass
class Parsed:
    """JSON 파싱 결과. 원문을 보관하지 않는다."""

    ok: bool
    data: Any = field(default=None, repr=False)
    reason: Reason = Reason.NONE


def parse_json(raw: RawResult) -> Parsed:
    text = raw.stdout.strip()
    if not text:
        return Parsed(False, reason=Reason.EMPTY_RESPONSE)
    decoded = None
    failed = False
    try:
        decoded = json.loads(text)
    except ValueError:
        failed = True
    if failed:
        # except 블록 밖: 디코딩 예외의 doc(원문)를 보관하지 않는다.
        return Parsed(False, reason=Reason.JSON_ERROR)
    return Parsed(True, data=decoded)


def parse_object(raw: RawResult) -> Parsed:
    """JSON 객체(dict)만 통과시킨다. 배열·스칼라는 계약 위반이다."""
    parsed = parse_json(raw)
    if not parsed.ok:
        return parsed
    if not isinstance(parsed.data, dict):
        return Parsed(False, reason=Reason.NOT_OBJECT)
    return parsed


def validate_status(raw: RawResult) -> Parsed:
    """`status`: 객체이며 문자열 `pipe`가 있어야 한다."""
    parsed = parse_object(raw)
    if not parsed.ok:
        return parsed
    if not isinstance(parsed.data.get("pipe"), str):
        return Parsed(False, reason=Reason.MISSING_FIELD)
    return parsed


def validate_capabilities(raw: RawResult) -> Parsed:
    """`capabilities`: `commands` 배열이거나 최상위 `loading=true`(정상, 대기 재시도)."""
    parsed = parse_object(raw)
    if not parsed.ok:
        return parsed
    data = parsed.data
    if data.get("loading") is True:
        return parsed
    if not isinstance(data.get("commands"), list):
        return Parsed(False, reason=Reason.MISSING_FIELD)
    return parsed


_VALIDATORS = {
    "status": validate_status,
    "capabilities": validate_capabilities,
}


def validate_query(command: str, raw: RawResult) -> Parsed:
    """명령에 맞는 검증기를 고른다. 등록되지 않은 조회는 객체 여부만 검사한다."""
    validator = _VALIDATORS.get(command, parse_object)
    return validator(raw)


def find_markers(data: Any, key: str) -> list:
    """최상위와 body 안의 필드 값을 모두 모은다(한쪽이 null이어도 다른 쪽을 놓치지 않는다)."""
    values = []
    if not isinstance(data, dict):
        return values
    if key in data:
        values.append(data[key])
    body = data.get("body")
    if isinstance(body, dict) and key in body:
        values.append(body[key])
    return values


def find_marker(data: Any, key: str) -> Optional[Any]:
    """비어 있지 않은 첫 값을 돌려준다. 없으면 None."""
    for value in find_markers(data, key):
        if value not in (None, "", False):
            return value
    return None
