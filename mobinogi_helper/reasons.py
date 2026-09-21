"""공용 사유 열거. 3단계 감독기가 이 값을 상태 전이로 옮긴다(02 §5)."""

from __future__ import annotations

from enum import Enum


class Failure(str, Enum):
    """어댑터가 관찰한 사실. 의미 판정은 하지 않는다(02 §6.1)."""

    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"
    OUTPUT_TOO_LARGE = "output_too_large"
    INTERRUPTED = "interrupted"


class Reason(str, Enum):
    """판정기·검증기가 붙이는 사유."""

    NONE = "none"
    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"
    OUTPUT_TOO_LARGE = "output_too_large"
    USER_INTERRUPT = "user_interrupt"
    PROCESS_EXIT = "process_exit"
    CODE_5 = "code_5"
    EMPTY_RESPONSE = "empty_response"
    JSON_ERROR = "json_error"
    NOT_OBJECT = "not_object"
    MISSING_FIELD = "missing_field"
    NOT_STRING_STATUS = "not_string_status"
    UNKNOWN_STATUS = "unknown_status"
    NOT_SUCCESS_MARKER = "not_success_marker"
    NO_KNOWN_RESPONSES = "no_known_responses"
    REJECTED = "rejected"
    SESSION_ABSENT = "session_absent"
    STALE_GENERATION = "stale_generation"
    BUSY = "busy"

    @property
    def is_protocol_error(self) -> bool:
        """응답 계약 위반(02 G13)에 해당하는 사유인지."""
        return self in {
            Reason.EMPTY_RESPONSE,
            Reason.JSON_ERROR,
            Reason.NOT_OBJECT,
            Reason.MISSING_FIELD,
        }
