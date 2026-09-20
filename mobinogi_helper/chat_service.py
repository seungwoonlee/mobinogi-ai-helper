"""채팅 실행 파이프라인(05 §4.5). 브로커 위에서 write-ahead 기록·판정·재전송 확인을 한다."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
import time
from typing import Callable, Optional

from .action_context import (
    ActionContext,
    ActionStore,
    Outcome,
    Step,
    StepState,
    StoreError,
    new_action_id,
    process_start_time,
)
from .broker import CommandBroker
from .chat_input import InputJudgment
from .chat_plan import ChatPlan, build_chat_plan
from .command_catalog import Kind, is_allowed_behaviour
from .origin import Approval, Origin, Provenance, check_send_allowed
from .reasons import Reason
from .response_judge import KnownResponses, SendVerdict, VerdictKind, judge_write_chat

DUPLICATE_WINDOW_SECONDS = 60.0  # [후보]
BEHAVIOUR_WAIT_MS = 5000


class ChatStatus(str, Enum):
    DONE = "done"
    REFUSED = "refused"
    INVALID_INPUT = "invalid_input"
    NEEDS_DUPLICATE_CONFIRM = "needs_duplicate_confirm"
    BUSY = "busy"
    STORE_FAILED = "store_failed"


@dataclass
class ChatResult:
    status: ChatStatus
    outcome: Optional[Outcome] = None
    reason: Reason = Reason.NONE
    detail: str = ""
    context_id: Optional[str] = None
    behaviour_resend_allowed: bool = False
    chat_verdict: Optional[SendVerdict] = None
    behaviour_verdict: Optional[SendVerdict] = None
    store_warning: bool = False


def _payload(text: str) -> str:
    return "base64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ChatService:
    def __init__(
        self,
        broker: CommandBroker,
        store: ActionStore,
        known: KnownResponses,
        *,
        auto_behaviour: bool = True,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        duplicate_window: float = DUPLICATE_WINDOW_SECONDS,
    ) -> None:
        self._broker = broker
        self._store = store
        self._known = known
        self._auto_behaviour = auto_behaviour
        self._clock = clock
        self._wall = wall
        self._window = duplicate_window
        self._recent: dict = {}  # 메모리 해시 → 등록 시각(프로세스 안에서만 유효)

    # ---- 재전송 확인 ---------------------------------------------------
    def plan_for(self, judgment: InputJudgment, origin: Origin) -> ChatPlan:
        """미리보기에 보일 최종 전송 내용."""
        assert judgment.message is not None
        if origin == Origin.USER_AT:
            return build_chat_plan(judgment.message, self._auto_behaviour)
        return ChatPlan(judgment.message, None)

    def needs_duplicate_confirm(self, text: str) -> bool:
        """동일 문구 60초 이내, 또는 확인하지 않은 UNRESOLVED(판정 불가 소유자 포함)가 있는가."""
        now = self._clock()
        for key, stamp in list(self._recent.items()):
            if now - stamp > self._window:
                del self._recent[key]
        if _digest(text) in self._recent:
            return True
        if self._store.unacknowledged_unresolved():
            return True
        return self._store.has_undetermined_pending()

    def acknowledge_previous(self) -> int:
        """사용자가 채팅창을 확인했다고 답한 뒤 호출한다. 저장 실패 시 StoreError."""
        return self._store.acknowledge_unresolved()

    # ---- 채팅 전송 -----------------------------------------------------
    def send_chat_action(
        self,
        judgment: InputJudgment,
        *,
        origin: Optional[Origin],
        provenance: Provenance,
        paste_confirmed: bool = False,
        duplicate_confirmed: bool = False,
        approval: Optional[Approval] = None,
        action_id: Optional[str] = None,
    ) -> ChatResult:
        if not judgment.is_chat or judgment.message is None:
            return ChatResult(ChatStatus.INVALID_INPUT)

        decision = check_send_allowed(
            origin,
            provenance,
            Kind.CHAT,
            paste_confirmed=paste_confirmed,
            approval=approval,
            action_id=action_id or "",
            clock=self._clock,
        )
        if not decision.allowed:
            return ChatResult(ChatStatus.REFUSED, detail=decision.reason)
        assert origin is not None

        plan = self.plan_for(judgment, origin)
        if self.needs_duplicate_confirm(plan.text):
            if not duplicate_confirmed:
                return ChatResult(ChatStatus.NEEDS_DUPLICATE_CONFIRM)
            try:
                self.acknowledge_previous()
            except StoreError:
                return ChatResult(ChatStatus.STORE_FAILED, detail="확인 기록을 저장하지 못했어요")

        context = ActionContext(
            action_id=new_action_id(),
            kind=Kind.CHAT,
            label="게임 채팅 전송",
            origin=origin.value,
            command="write_chat",
            created_at=self._wall(),
            owner_pid=os.getpid(),
            owner_start=process_start_time(os.getpid()),
            steps=[Step("chat")],
        )

        chat_verdict, busy, store_failed = self._run_step(context, "chat", plan.text, wait_ms=0)
        if busy:
            return ChatResult(ChatStatus.BUSY, context_id=None)
        if store_failed:
            return ChatResult(ChatStatus.STORE_FAILED, detail="작업 기록을 저장하지 못해 전송하지 않았어요")
        assert chat_verdict is not None

        if chat_verdict.kind == VerdictKind.NOT_SENT:
            self._recent.pop(_digest(plan.text), None)
        else:
            self._recent[_digest(plan.text)] = self._clock()

        behaviour_verdict: Optional[SendVerdict] = None
        store_warning = False
        if chat_verdict.kind == VerdictKind.SENT and plan.behaviour:
            context.steps.append(Step("behaviour"))
            behaviour_verdict, busy_b, failed_b = self._run_step(
                context, "behaviour", plan.behaviour, wait_ms=BEHAVIOUR_WAIT_MS
            )
            if busy_b or failed_b:
                # 행동은 시도하지 못했다(채팅은 이미 나감). 재전송 가능한 실패로 기록한다.
                context.step("behaviour").state = StepState.FAILED
                behaviour_verdict = None
                store_warning = failed_b
                behaviour_attempted = False
            else:
                behaviour_attempted = True
        else:
            behaviour_attempted = False

        outcome, resend = self._outcome(chat_verdict, plan, behaviour_verdict, behaviour_attempted)
        context.outcome = outcome
        context.behaviour_resend_allowed = resend
        context.ended_at = self._wall()
        try:
            self._store.save(context)
        except StoreError:
            store_warning = True

        return ChatResult(
            ChatStatus.DONE,
            outcome=outcome,
            reason=chat_verdict.reason,
            context_id=context.action_id,
            behaviour_resend_allowed=resend,
            chat_verdict=chat_verdict,
            behaviour_verdict=behaviour_verdict,
            store_warning=store_warning,
        )

    def _run_step(self, context: ActionContext, name: str, text: str, wait_ms: int):
        """단계 하나를 write-ahead로 실행한다. (판정, busy 여부, 저장 실패 여부)를 돌려준다."""
        step = context.step(name)
        assert step is not None
        step.state = StepState.PENDING
        if context.started_at is None:
            context.started_at = self._wall()

        def before() -> None:
            self._store.save(context)  # 전송 직전에 pending 기록. 실패하면 전송하지 않는다.

        store_failed = False
        try:
            run = self._broker.run_send("write_chat", [_payload(text)], wait_ms=wait_ms, before=before)
        except StoreError:
            return None, False, True
        if run.busy:
            return None, True, False
        assert run.raw is not None
        verdict = judge_write_chat(run.raw, self._known)
        step.state = {
            VerdictKind.SENT: StepState.SENT,
            VerdictKind.NOT_SENT: StepState.FAILED,
            VerdictKind.UNKNOWN: StepState.UNKNOWN,
        }[verdict.kind]
        try:
            self._store.save(context)
        except StoreError:
            store_failed = False  # 이미 전송했으므로 결과만 보고한다(호출자가 store_warning을 본다)
        return verdict, False, store_failed

    @staticmethod
    def _outcome(chat: SendVerdict, plan: ChatPlan, behaviour: Optional[SendVerdict], attempted: bool):
        if chat.kind == VerdictKind.NOT_SENT:
            return Outcome.FAILED, False
        if chat.kind == VerdictKind.UNKNOWN:
            return Outcome.UNRESOLVED, False
        if not plan.behaviour:
            return Outcome.COMPLETED, False
        if not attempted:
            return Outcome.PARTIAL, True
        assert behaviour is not None
        if behaviour.kind == VerdictKind.SENT:
            return Outcome.COMPLETED, False
        if behaviour.kind == VerdictKind.NOT_SENT:
            return Outcome.PARTIAL, True
        return Outcome.PARTIAL, False  # 행동 결과 불명: 재전송 불가(중복 위험)

    # ---- 행동만 다시 보내기 ---------------------------------------------
    def resend_behaviour(
        self,
        context_id: str,
        behaviour: str,
        *,
        origin: Optional[Origin],
        provenance: Provenance,
    ) -> ChatResult:
        if not is_allowed_behaviour(behaviour):
            return ChatResult(ChatStatus.REFUSED, detail="허용된 행동이 아니에요")
        decision = check_send_allowed(origin, provenance, Kind.BEHAVIOUR, clock=self._clock)
        if not decision.allowed:
            return ChatResult(ChatStatus.REFUSED, detail=decision.reason)

        context = self._store.get(context_id)
        if (
            context is None
            or context.outcome != Outcome.PARTIAL
            or not context.behaviour_resend_allowed
        ):
            return ChatResult(ChatStatus.REFUSED, detail="다시 보낼 수 있는 행동이 아니에요")

        step = context.step("behaviour")
        if step is None:
            return ChatResult(ChatStatus.REFUSED, detail="다시 보낼 수 있는 행동이 아니에요")
        verdict, busy, store_failed = self._run_step(context, "behaviour", behaviour, wait_ms=BEHAVIOUR_WAIT_MS)
        if busy:
            return ChatResult(ChatStatus.BUSY, context_id=context_id)
        if store_failed:
            return ChatResult(ChatStatus.STORE_FAILED, context_id=context_id)
        assert verdict is not None
        if verdict.kind == VerdictKind.SENT:
            context.outcome = Outcome.COMPLETED
            context.behaviour_resend_allowed = False
        elif verdict.kind == VerdictKind.NOT_SENT:
            context.behaviour_resend_allowed = True
        else:
            context.behaviour_resend_allowed = False
        context.ended_at = self._wall()
        warning = False
        try:
            self._store.save(context)
        except StoreError:
            warning = True
        return ChatResult(
            ChatStatus.DONE,
            outcome=context.outcome,
            reason=verdict.reason,
            context_id=context_id,
            behaviour_resend_allowed=context.behaviour_resend_allowed,
            behaviour_verdict=verdict,
            store_warning=warning,
        )
