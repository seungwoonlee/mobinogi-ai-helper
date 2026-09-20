"""게임 채팅용 명령행 도구(구현 2단계: 기반 위의 얇은 껍데기).

실행 후 ``@@ 보낼 문구``를 입력하면 최종 전송 내용을 보여준 뒤 확인을 받고 게임 채팅으로 보낸다.
이 도구는 게임의 성공 응답 형식을 아직 배우지 못해(config/known_responses.json이 비어 있음)
모든 전송이 "확인 불가"로 안내된다. 실제로는 전달됐을 수 있으니 게임 채팅창에서 확인한다.

종료 코드: 0 전송 확정, 1 예기치 않은 오류, 2 입력 거부·취소, 3 확인 불가, 4 실패(미전송 확정),
5 일부 완료, 6 터미널이 아니라서 거부, 7 재전송 확인 미충족.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, TextIO

from mobinogi_helper.action_context import ActionStore, Outcome, StoreError
from mobinogi_helper.broker import CommandBroker
from mobinogi_helper.chat_input import CHAT, NOT_CHAT, REJECTED, judge_input, parse_chat_input, reject_message
from mobinogi_helper.chat_plan import build_chat_plan
from mobinogi_helper.chat_service import ChatResult, ChatService, ChatStatus
from mobinogi_helper.cli_adapter import CliNotFound, adapter_for, locate_cli
from mobinogi_helper.origin import Origin, Provenance
from mobinogi_helper.reasons import Reason
from mobinogi_helper.response_judge import load_known_responses

__all__ = ["Tool", "main", "parse_chat_input", "build_chat_plan", "safe_print"]

Q1 = "보낼까요? [y/N] "
Q2 = "이미 전송됐을 수 있어요. 게임 채팅창에서 확인했나요? 그래도 다시 보낼까요? [y/N] "

MSG_UNKNOWN_NO_SAMPLES = (
    "전송됐을 수 있지만 결과를 확인할 수 없어요. 이 도구는 아직 게임의 성공·거부 응답 형식을 "
    "배우지 못했어요(표본 미확정). 게임 채팅창에서 확인해주세요. 다시 보내지 않았어요."
)
MSG_UNKNOWN = "전송됐는지 확인할 수 없어요. 다시 보내지 않았어요. 게임 채팅창에서 확인해주세요."


def safe_print(text: str, stream: Optional[TextIO] = None) -> None:
    """출력 인코딩 때문에 예외가 나서 전송 결과가 실패로 오해되는 일이 없게 쓴다."""
    target = stream if stream is not None else sys.stdout
    try:
        target.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or "utf-8"
        target.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")
    try:
        target.flush()
    except (OSError, ValueError):
        pass


def _drain_input() -> None:
    """붙여넣기로 남은 입력이 확인 질문의 답으로 소비되지 않도록 버퍼를 비운다(가능한 범위)."""
    try:
        import msvcrt  # type: ignore

        while msvcrt.kbhit():
            msvcrt.getwch()
    except ImportError:
        pass


def _ask_real(prompt: str) -> str:
    _drain_input()
    try:
        return input(prompt)
    except EOFError:
        return ""  # EOF는 취소로 처리한다


def _is_yes(answer: str) -> bool:
    return answer.strip() in {"y", "Y"}


class Tool:
    def __init__(
        self,
        service: ChatService,
        *,
        ask: Callable[[str], str] = _ask_real,
        out: Optional[TextIO] = None,
    ) -> None:
        self._service = service
        self._ask = ask
        self._out = out

    def say(self, text: str) -> None:
        safe_print(text, self._out)

    def handle_line(self, line: str) -> int:
        judgment = judge_input(line)
        if judgment.kind == NOT_CHAT:
            self.say("형식: @@ 보낼 문구")
            return 2
        if judgment.kind == REJECTED:
            self.say(reject_message(judgment))
            return 2
        assert judgment.kind == CHAT

        plan = self._service.plan_for(judgment, Origin.USER_AT)
        preview = f"보낼 말: {plan.text}" + (f" (행동: {plan.behaviour})" if plan.behaviour else "")
        self.say(preview)
        if judgment.normalized:
            self.say("입력한 문구가 정규화되어 위 내용으로 보내져요.")
        self.say("다른 플레이어에게 보이는 게임 채팅이에요.")

        duplicate_confirmed = False
        if self._service.needs_duplicate_confirm(plan.text):
            if not _is_yes(self._ask(Q2)):
                self.say("취소했어요. 게임 채팅창을 먼저 확인해주세요.")
                return 7
            try:
                self._service.acknowledge_previous()
            except StoreError:
                self.say("확인 기록을 저장하지 못해 전송하지 않았어요.")
                return 1
            duplicate_confirmed = True

        if not _is_yes(self._ask(Q1)):
            self.say("취소했어요.")
            return 2

        # input()은 타이핑과 붙여넣기를 구분하지 못하므로 확인을 거친 PASTED로 처리한다(05 §4.10).
        result = self._service.send_chat_action(
            judgment,
            origin=Origin.USER_AT,
            provenance=Provenance.PASTED,
            paste_confirmed=True,
            duplicate_confirmed=duplicate_confirmed,
        )
        return self._report(result, plan.text, plan.behaviour)

    def _report(self, result: ChatResult, text: str, behaviour: Optional[str]) -> int:
        if result.store_warning:
            self.say("작업 기록을 저장하지 못했어요. 다음 실행의 재전송 확인이 정확하지 않을 수 있어요.")
        if result.status == ChatStatus.REFUSED:
            self.say(f"보내지 않았어요: {result.detail}")
            return 2
        if result.status == ChatStatus.NEEDS_DUPLICATE_CONFIRM:
            self.say("재전송 확인이 필요해요. 보내지 않았어요.")
            return 7
        if result.status == ChatStatus.BUSY:
            self.say("다른 전송이 진행 중이라 보내지 않았어요.")
            return 1
        if result.status in (ChatStatus.STORE_FAILED, ChatStatus.INVALID_INPUT):
            self.say(result.detail or "보내지 않았어요.")
            return 1

        outcome = result.outcome
        if outcome == Outcome.COMPLETED:
            suffix = f" + {behaviour}" if behaviour else ""
            self.say(f"전송 완료: {text}{suffix}")
            return 0
        if outcome == Outcome.PARTIAL:
            if result.behaviour_resend_allowed:
                self.say("채팅은 보냈고 행동만 실패했어요. 채팅은 다시 보내지 않아요.")
            else:
                self.say("채팅은 보냈어요. 행동은 됐는지 알 수 없어요.")
            return 5
        if outcome == Outcome.FAILED:
            self.say("채팅을 보내지 못했어요.")
            return 4
        if result.reason == Reason.NO_KNOWN_RESPONSES:
            self.say(MSG_UNKNOWN_NO_SAMPLES)
        else:
            self.say(MSG_UNKNOWN)
        return 3

    def run_interactive(self) -> int:
        self.say("게임 채팅 도구입니다. @@ 뒤에 문구를 입력하세요. 종료: exit 또는 quit")
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                self.say("\n종료합니다.")
                return 0
            if line.strip().lower() in {"exit", "quit"}:
                return 0
            self.handle_line(line)


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def build_tool() -> Optional[Tool]:
    try:
        path = locate_cli()
    except CliNotFound as error:
        safe_print(f"오류: {error}", sys.stderr)
        return None
    known, problem = load_known_responses()
    if problem:
        safe_print(f"알림: {problem} 모든 전송이 확인 불가로 안내돼요.", sys.stderr)
    store = ActionStore()
    store.close_orphans()
    service = ChatService(CommandBroker(adapter_for(path)), store, known)
    return Tool(service)


def main(argv: list[str]) -> int:
    if not _stdin_is_terminal():
        safe_print(
            "터미널에서 직접 실행해야 해요. 스크립트나 파이프로는 채팅을 보내지 않아요.",
            sys.stderr,
        )
        return 6
    tool = build_tool()
    if tool is None:
        return 1
    if argv:
        return tool.handle_line(" ".join(argv))
    return tool.run_interactive()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
