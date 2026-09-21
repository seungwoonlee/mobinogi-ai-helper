"""최소 CommandBroker: 명령 직렬화, 요청 ID, 세대 번호, 명령 상태(02 §5.3, §6.3).

채팅 의미는 모른다. 3단계 감독기는 관찰자 콜백으로 상태 전이를 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
from typing import Any, Callable, Optional, Sequence
import uuid

from .cli_adapter import CliAdapter, RawResult
from .reasons import Failure, Reason
from .response_validator import validate_query

QUERY_TIMEOUT = 5.0   # [후보] 02 §8.1
SEND_TIMEOUT = 15.0   # [후보] 03 §6.6


class CommandState(str, Enum):
    IDLE = "IDLE"
    RUNNING_QUERY = "RUNNING_QUERY"
    RUNNING_SEND = "RUNNING_SEND"
    FAILED = "FAILED"


class QueryKind(str, Enum):
    OK = "ok"
    FAILED = "failed"
    STALE = "stale"
    BUSY = "busy"


@dataclass
class QueryResult:
    kind: QueryKind
    reason: Reason
    generation: int
    request_id: str
    data: Any = field(default=None, repr=False)


@dataclass
class SendRun:
    busy: bool
    raw: Optional[RawResult] = None
    reason: Reason = Reason.NONE


Observer = Callable[[CommandState, CommandState, Reason], None]


class CommandBroker:
    def __init__(
        self,
        adapter: CliAdapter,
        query_timeout: float = QUERY_TIMEOUT,
        send_timeout: float = SEND_TIMEOUT,
        session_absent: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self._adapter = adapter
        self._query_timeout = query_timeout
        self._send_timeout = send_timeout
        # [가정] 캐릭터 미접속 응답의 형식은 미확인이다(02 §18). 판정 함수를 주입한다.
        self._session_absent = session_absent
        self._lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self._state = CommandState.IDLE
        self._generation = 0
        self._observers: list = []

    @property
    def state(self) -> CommandState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        """절전 복귀·재연결 등에서 3단계 감독기가 호출한다. 이전 세대의 QUERY 응답은 폐기된다."""
        with self._gen_lock:
            self._generation += 1
            return self._generation

    def add_observer(self, observer: Observer) -> None:
        self._observers.append(observer)

    def _transition(self, new: CommandState, reason: Reason = Reason.NONE) -> None:
        previous, self._state = self._state, new
        for observer in list(self._observers):
            try:
                observer(previous, new, reason)
            except Exception:  # noqa: BLE001 - 관찰자 오류가 실행 결과를 잃게 하지 않는다
                pass

    def _acquire(self, wait_ms: int) -> bool:
        return self._lock.acquire(timeout=max(wait_ms, 0) / 1000.0)

    def run_query(self, command: str, args: Sequence[str] = (), wait_ms: int = 0) -> QueryResult:
        request_id = uuid.uuid4().hex
        generation = self._generation
        if not self._acquire(wait_ms):
            return QueryResult(QueryKind.BUSY, Reason.BUSY, generation, request_id)
        try:
            self._transition(CommandState.RUNNING_QUERY)
            raw = self._adapter.run(command, args, timeout=self._query_timeout)
            result = self._classify_query(command, raw, generation, request_id)
            self._transition(CommandState.IDLE, result.reason)
            return result
        finally:
            self._state = CommandState.IDLE  # 예외가 나도 RUNNING에 고착되지 않는다
            self._lock.release()

    def _classify_query(self, command: str, raw: RawResult, generation: int, request_id: str) -> QueryResult:
        def failed(reason: Reason) -> QueryResult:
            return QueryResult(QueryKind.FAILED, reason, generation, request_id)

        # 오래된 세대의 응답은 성공·실패를 가리지 않고 폐기한다(02 §5 세대 번호 규칙).
        if generation != self._generation:
            return QueryResult(QueryKind.STALE, Reason.STALE_GENERATION, generation, request_id)
        if raw.failure == Failure.TIMEOUT:
            return failed(Reason.TIMEOUT)
        if raw.failure == Failure.INTERRUPTED:
            return failed(Reason.USER_INTERRUPT)
        if raw.failure == Failure.LAUNCH_FAILED:
            return failed(Reason.LAUNCH_FAILED)
        if raw.failure == Failure.OUTPUT_TOO_LARGE:
            return failed(Reason.OUTPUT_TOO_LARGE)
        if raw.exit_code == 5:
            return failed(Reason.CODE_5)
        if raw.exit_code != 0:
            return failed(Reason.PROCESS_EXIT)

        parsed = validate_query(command, raw)
        if not parsed.ok:
            return failed(parsed.reason)
        if self._session_absent is not None and self._session_absent(parsed.data):
            return failed(Reason.SESSION_ABSENT)
        return QueryResult(QueryKind.OK, Reason.NONE, generation, request_id, data=parsed.data)

    def run_send(
        self,
        command: str,
        args: Sequence[str],
        wait_ms: int = 0,
        before: Optional[Callable[[], None]] = None,
        timeout: Optional[float] = None,
        evaluate: Optional[Callable[[RawResult], Reason]] = None,
    ) -> SendRun:
        """채팅·행동 전송. `before`는 잠금을 잡은 뒤 프로세스를 시작하기 전에 호출된다(write-ahead).

        `evaluate`는 호출자가 결과를 판정해 사유를 돌려주는 콜백이다(브로커는 채팅 의미를 모른다).
        사유가 `NONE`이 아니면 관찰자는 `FAILED` → `IDLE` 전이를 사유와 함께 받는다(02 C16·C17).
        """
        if not self._acquire(wait_ms):
            return SendRun(busy=True)
        try:
            if before is not None:
                before()  # 예외가 나면 전송하지 않는다
            self._transition(CommandState.RUNNING_SEND)
            raw = self._adapter.run(command, args, timeout=self._send_timeout if timeout is None else timeout)
            reason = Reason.NONE
            if evaluate is not None:
                try:
                    reason = evaluate(raw)
                except Exception:  # noqa: BLE001 - 판정 콜백 오류가 결과를 잃게 하지 않는다
                    reason = Reason.NONE
            if reason != Reason.NONE:
                self._transition(CommandState.FAILED, reason)
            self._transition(CommandState.IDLE, reason)
            return SendRun(busy=False, raw=raw, reason=reason)
        finally:
            self._state = CommandState.IDLE  # 예외가 나도 RUNNING에 고착되지 않는다
            self._lock.release()
