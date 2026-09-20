"""작업 컨텍스트와 저장(03 §3·§4). 채팅 문구·argv·stdout 원문은 저장하지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import time
import uuid
from typing import Callable, Iterable, Optional

from .command_catalog import Kind

SCHEMA_VERSION = 1
ENV_STORE = "MOBINOGI_STORE_DIR"


class Outcome(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"
    UNRESOLVED = "UNRESOLVED"


class StepState(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OwnerState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class StoreError(Exception):
    """저장 실패. write-ahead 기록에 실패하면 전송하지 않는다."""


@dataclass
class Step:
    name: str
    state: StepState = StepState.PENDING


@dataclass
class ActionContext:
    action_id: str
    kind: Kind
    label: str
    origin: str
    command: str
    created_at: float
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    steps: list = field(default_factory=list)
    outcome: Optional[Outcome] = None
    owner_pid: int = 0
    owner_start: Optional[int] = None
    acknowledged: bool = False
    behaviour_resend_allowed: bool = False

    def step(self, name: str) -> Optional[Step]:
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        return None

    def has_pending(self) -> bool:
        return any(step.state == StepState.PENDING for step in self.steps)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "action_id": self.action_id,
            "kind": self.kind.value,
            "label": self.label,
            "origin": self.origin,
            "command": self.command,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [{"name": s.name, "state": s.state.value} for s in self.steps],
            "outcome": self.outcome.value if self.outcome else None,
            "owner_pid": self.owner_pid,
            "owner_start": self.owner_start,
            "acknowledged": self.acknowledged,
            "behaviour_resend_allowed": self.behaviour_resend_allowed,
        }

    @staticmethod
    def from_dict(data: dict) -> "ActionContext":
        return ActionContext(
            action_id=data["action_id"],
            kind=Kind(data["kind"]),
            label=data["label"],
            origin=data["origin"],
            command=data["command"],
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            steps=[Step(s["name"], StepState(s["state"])) for s in data.get("steps", [])],
            outcome=Outcome(data["outcome"]) if data.get("outcome") else None,
            owner_pid=data.get("owner_pid", 0),
            owner_start=data.get("owner_start"),
            acknowledged=data.get("acknowledged", False),
            behaviour_resend_allowed=data.get("behaviour_resend_allowed", False),
        )


def new_action_id() -> str:
    return uuid.uuid4().hex


def default_store_dir() -> Path:
    configured = os.environ.get(ENV_STORE)
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "mobinogi-ai-helper" / "actions"


def process_start_time(pid: int) -> Optional[int]:
    """프로세스 시작 시각(Windows FILETIME). 얻지 못하면 None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            unused = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(unused), ctypes.byref(unused), ctypes.byref(unused)):
                return None
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 판정 불가로 처리
        return None


def _windows_owner_state(pid: int, start: Optional[int]) -> OwnerState:
    """Windows: 프로세스 없음(ERROR_INVALID_PARAMETER)은 죽음, 접근 불가는 판정 불가로 구분한다."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: 해당 PID의 프로세스가 없다
                return OwnerState.DEAD
            return OwnerState.UNKNOWN  # 접근 거부 등
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                return OwnerState.DEAD
            creation = wintypes.FILETIME()
            unused = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(unused), ctypes.byref(unused), ctypes.byref(unused)
            ):
                return OwnerState.UNKNOWN
            current = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            if start is None:
                return OwnerState.UNKNOWN  # 시작 시각이 없으면 PID 재사용을 배제할 수 없다
            return OwnerState.ALIVE if current == start else OwnerState.DEAD  # 다르면 PID가 재사용됨
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 판정 불가로 처리
        return OwnerState.UNKNOWN


def owner_state(pid: int, start: Optional[int]) -> OwnerState:
    """소유 프로세스의 생사를 판정한다. 알 수 없으면 UNKNOWN(기록을 바꾸지 않는다)."""
    if pid <= 0:
        return OwnerState.UNKNOWN
    if pid == os.getpid():
        return OwnerState.ALIVE
    if os.name == "nt":
        return _windows_owner_state(pid, start)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return OwnerState.DEAD
    except PermissionError:
        return OwnerState.ALIVE
    except OSError:
        return OwnerState.UNKNOWN
    return OwnerState.ALIVE


class ActionStore:
    """컨텍스트마다 파일 하나(`<action_id>.json`)로 저장한다. 원자적 쓰기(임시 파일 → 교체)."""

    def __init__(self, directory: Optional[Path] = None, owner_probe: Callable[[int, Optional[int]], OwnerState] = owner_state) -> None:
        self._dir = Path(directory) if directory else default_store_dir()
        self._owner_probe = owner_probe

    @property
    def directory(self) -> Path:
        return self._dir

    def save(self, context: ActionContext) -> None:
        payload = json.dumps(context.to_dict(), ensure_ascii=False)
        target = self._dir / f"{context.action_id}.json"
        temp = self._dir / f"{context.action_id}.tmp"
        failed = False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except OSError:
            failed = True
        if failed:
            raise StoreError("작업 기록을 저장하지 못했어요")

    def load_all(self) -> list:
        contexts = []
        if not self._dir.is_dir():
            return contexts
        for path in sorted(self._dir.glob("*.json")):
            context = None
            bad = False
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    context = ActionContext.from_dict(json.load(handle))
            except OSError:
                continue  # 다른 프로세스가 쓰는 중일 수 있다: 옮기지 않고 다음에 다시 읽는다
            except (ValueError, KeyError, TypeError):
                bad = True
            if bad:
                try:
                    os.replace(path, path.with_suffix(".corrupt"))
                except OSError:
                    pass
                continue
            contexts.append(context)
        return contexts

    def get(self, action_id: str) -> Optional[ActionContext]:
        for context in self.load_all():
            if context.action_id == action_id:
                return context
        return None

    def unacknowledged_unresolved(self, kinds: Iterable[Kind] = (Kind.CHAT, Kind.BEHAVIOUR)) -> list:
        wanted = set(kinds)
        return [
            c for c in self.load_all()
            if c.outcome == Outcome.UNRESOLVED and not c.acknowledged and c.kind in wanted
        ]

    def acknowledge_unresolved(self, kinds: Iterable[Kind] = (Kind.CHAT, Kind.BEHAVIOUR)) -> int:
        count = 0
        for context in self.unacknowledged_unresolved(kinds):
            context.acknowledged = True
            self.save(context)
            count += 1
        return count

    def close_orphans(self) -> dict:
        """시작 시 호출: 소유 프로세스가 죽은 미완료 컨텍스트만 UNRESOLVED로 마감한다.

        판정 불가(UNKNOWN)인 소유자의 기록은 바꾸지 않고, 확인하지 않은 기록처럼 남는다.
        """
        closed = 0
        undetermined = 0
        for context in self.load_all():
            if not context.has_pending():
                continue
            state = self._owner_probe(context.owner_pid, context.owner_start)
            if state == OwnerState.ALIVE:
                continue
            if state == OwnerState.UNKNOWN:
                undetermined += 1
                continue
            for step in context.steps:
                if step.state == StepState.PENDING:
                    step.state = StepState.UNKNOWN
            if context.outcome is None:
                context.outcome = Outcome.UNRESOLVED
            context.behaviour_resend_allowed = False  # 이미 나갔을 수 있어 재전송을 허용하지 않는다
            context.ended_at = time.time()
            self.save(context)
            closed += 1
        return {"closed": closed, "undetermined": undetermined}

    def has_undetermined_pending(self) -> bool:
        """판정 불가 소유자의 미완료 기록이 있는지(질문 2 조건에 포함)."""
        for context in self.load_all():
            if context.has_pending():
                if self._owner_probe(context.owner_pid, context.owner_start) == OwnerState.UNKNOWN:
                    return True
        return False
