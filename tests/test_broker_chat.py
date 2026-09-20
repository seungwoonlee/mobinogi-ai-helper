import base64
import json
import random
import threading
import time
import unittest

from mobinogi_helper.action_context import Outcome, StepState
from mobinogi_helper.broker import CommandState, QueryKind
from mobinogi_helper.chat_input import judge_input
from mobinogi_helper.chat_service import ChatStatus
from mobinogi_helper.origin import Origin, Provenance
from mobinogi_helper.reasons import Reason
from mobinogi_helper.response_judge import KnownResponses

from .support import KNOWN_FIXTURE, SENT, FakeCliTestCase, tag

EMPTY = KnownResponses()


def user_typed(service, line, **kw):
    return service.send_chat_action(
        judge_input(line), origin=Origin.USER_AT, provenance=Provenance.TYPED, **kw
    )


def decoded_sent_texts(calls):
    texts = []
    for call in calls:
        argv = call["argv"]
        if argv and argv[0] == "write_chat":
            texts.append(base64.b64decode(argv[1].split(":", 1)[1]).decode("utf-8"))
    return texts


class BrokerQueryTests(FakeCliTestCase):
    @tag("C1", "C10")
    def test_C10_normal_query(self):
        self.scenario(stdout=json.dumps({"pipe": "connected"}))
        result = self.broker().run_query("status")
        self.assertEqual((result.kind, result.reason), (QueryKind.OK, Reason.NONE))

    @tag("C11")
    def test_C11_timeout(self):
        self.scenario(delay=5, stdout="{}")
        result = self.broker(query_timeout=0.3).run_query("status")
        self.assertEqual((result.kind, result.reason), (QueryKind.FAILED, Reason.TIMEOUT))

    @tag("C12")
    def test_C12_code_5(self):
        self.scenario(exit_code=5, stdout=json.dumps({"error": "game_off"}))
        result = self.broker().run_query("status")
        self.assertEqual(result.reason, Reason.CODE_5)

    @tag("C13")
    def test_C13_contract_violation(self):
        for text, reason in (("[1]", Reason.NOT_OBJECT), ("", Reason.EMPTY_RESPONSE), ("{", Reason.JSON_ERROR), ("{}", Reason.MISSING_FIELD)):
            with self.subTest(text=text):
                self.scenario(stdout=text)
                result = self.broker().run_query("status")
                self.assertEqual(result.reason, reason)
                self.assertTrue(result.reason.is_protocol_error)

    @tag("C14")
    def test_C14_process_exit(self):
        self.scenario(crash=True, exit_code=3, stdout="")
        result = self.broker().run_query("status")
        self.assertEqual(result.reason, Reason.PROCESS_EXIT)

    @tag("C36")
    def test_C36_session_absent_assumed_format(self):
        # [가정] 캐릭터 미접속 응답 형식은 미확인이라 판정 함수를 주입해 시험한다.
        self.scenario(stdout=json.dumps({"pipe": "connected", "session": None}))
        broker = self.broker(session_absent=lambda data: data.get("session") is None)
        self.assertEqual(broker.run_query("status").reason, Reason.SESSION_ABSENT)

    def test_loading_capabilities_is_ok_not_error(self):
        self.scenario(stdout=json.dumps({"loading": True}))
        self.assertEqual(self.broker().run_query("capabilities").kind, QueryKind.OK)

    @tag("G34")
    def test_G34_old_generation_query_is_discarded(self):
        self.scenario(delay=0.5, stdout=json.dumps({"pipe": "connected"}))
        broker = self.broker()
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault("r", broker.run_query("status")))
        thread.start()
        self.wait_for_calls(1)  # 모의 CLI가 시작한 것을 확인한 뒤 세대를 올린다
        broker.bump_generation()
        thread.join()
        self.assertEqual(holder["r"].kind, QueryKind.STALE)

    def test_observer_sees_state_transitions(self):
        self.scenario(stdout=json.dumps({"pipe": "connected"}))
        seen = []
        broker = self.broker()
        broker.add_observer(lambda a, b, r: seen.append((a, b)))
        broker.run_query("status")
        self.assertEqual(seen, [(CommandState.IDLE, CommandState.RUNNING_QUERY), (CommandState.RUNNING_QUERY, CommandState.IDLE)])

    @tag("T19")
    def test_T19_reentry_returns_busy_without_second_process(self):
        self.scenario(delay=0.6, stdout=SENT)
        broker = self.broker(send_timeout=5)
        first = threading.Thread(target=lambda: broker.run_send("write_chat", ["base64:QQ=="]))
        first.start()
        self.wait_for_calls(1)
        second = broker.run_send("write_chat", ["base64:QQ=="], wait_ms=0)
        self.assertTrue(second.busy)
        first.join()
        self.assertEqual(len(self.calls()), 1)


class ChatServiceTests(FakeCliTestCase):
    @tag("T1", "C2", "C15")
    def test_T1_success_completes_and_sends_emoji_text(self):
        self.scenario(stdout=SENT)
        result = user_typed(self.service(), "@@ 길드챗 테스트")
        self.assertEqual((result.status, result.outcome), (ChatStatus.DONE, Outcome.COMPLETED))
        self.assertEqual(decoded_sent_texts(self.calls()), ["길드챗 테스트 😊"])

    @tag("T7", "C32")
    def test_T7_chat_sent_behaviour_failed_is_partial_with_resend(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "rejected"})}])
        service = self.service()
        result = user_typed(service, "@@ 고마워요")
        self.assertEqual(result.outcome, Outcome.PARTIAL)
        self.assertTrue(result.behaviour_resend_allowed)
        self.assertEqual(decoded_sent_texts(self.calls()), ["고마워요 😍", "/하트"])

    @tag("T36", "C33")
    def test_T36_behaviour_unknown_is_partial_without_resend(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "weird"})}])
        result = user_typed(self.service(), "@@ 고마워요")
        self.assertEqual(result.outcome, Outcome.PARTIAL)
        self.assertFalse(result.behaviour_resend_allowed)
        again = self.service().resend_behaviour(
            result.context_id, "/하트", origin=Origin.USER_AT, provenance=Provenance.TYPED
        )
        self.assertEqual(again.status, ChatStatus.REFUSED)

    @tag("T35")
    def test_T35_resend_behaviour_only_never_repeats_chat(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "rejected"})}, {"stdout": SENT}])
        service = self.service()
        first = user_typed(service, "@@ 고마워요")
        result = service.resend_behaviour(first.context_id, "/하트", origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual((result.status, result.outcome), (ChatStatus.DONE, Outcome.COMPLETED))
        self.assertEqual(decoded_sent_texts(self.calls()), ["고마워요 😍", "/하트", "/하트"])

    @tag("T34")
    def test_T34_resend_rejects_behaviour_outside_allowlist(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "rejected"})}])
        service = self.service()
        first = user_typed(service, "@@ 고마워요")
        bad = service.resend_behaviour(first.context_id, "/지역", origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual(bad.status, ChatStatus.REFUSED)
        self.assertEqual(len(self.calls()), 2)

    @tag("T8", "C17")
    def test_T8_timeout_is_unresolved_and_behaviour_not_run(self):
        self.scenario(delay=5, stdout=SENT)
        service = self.service(broker=self.broker(send_timeout=0.3))
        result = user_typed(service, "@@ 고마워요")
        self.assertEqual((result.status, result.outcome), (ChatStatus.DONE, Outcome.UNRESOLVED))
        self.assertEqual(result.reason, Reason.TIMEOUT)
        self.assertEqual(len(self.calls()), 1)  # 행동은 실행되지 않았다

    @tag("T40")
    def test_T40_shipped_empty_config_never_confirms_and_skips_behaviour(self):
        self.scenario(stdout=SENT)
        result = user_typed(self.service(known=EMPTY), "@@ 고마워요")
        self.assertEqual(result.outcome, Outcome.UNRESOLVED)
        self.assertEqual(result.reason, Reason.NO_KNOWN_RESPONSES)
        self.assertEqual(len(self.calls()), 1)

    @tag("C16")
    def test_C16_not_sent_is_failed_and_unregisters_hash(self):
        self.scenario(stdout=json.dumps({"status": "rejected"}))
        service = self.service()
        result = user_typed(service, "@@ 안녕하세요")
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertFalse(service.needs_duplicate_confirm("안녕하세요 😊"))

    @tag("T43")
    def test_T43_game_rejects_long_text_is_failed(self):
        self.scenario(stdout=json.dumps({"status": "rejected", "error": "too_long"}))
        result = user_typed(self.service(), "@@ " + "가" * 48)
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(len(self.calls()), 1)

    @tag("T20")
    def test_T20_duplicate_within_window_needs_confirmation(self):
        self.scenario(stdout=SENT)
        service = self.service()
        user_typed(service, "@@ 길드챗 테스트")
        refused = user_typed(service, "@@ 길드챗 테스트")
        self.assertEqual(refused.status, ChatStatus.NEEDS_DUPLICATE_CONFIRM)
        self.assertEqual(len(self.calls()), 1)
        confirmed = user_typed(service, "@@ 길드챗 테스트", duplicate_confirmed=True)
        self.assertEqual(confirmed.status, ChatStatus.DONE)

    @tag("T20")
    def test_T20_window_expires(self):
        self.scenario(stdout=SENT)
        now = [0.0]
        service = self.service(clock=lambda: now[0])
        user_typed(service, "@@ 길드챗 테스트")
        now[0] = 61.0
        self.assertEqual(user_typed(service, "@@ 길드챗 테스트").status, ChatStatus.DONE)

    @tag("T20")
    def test_T20_unacknowledged_unresolved_requires_confirmation_and_is_released_by_ack(self):
        self.scenario(stdout=SENT)
        first = user_typed(self.service(known=EMPTY), "@@ 안녕하세요")
        self.assertEqual(first.outcome, Outcome.UNRESOLVED)
        fresh = self.service(known=KNOWN_FIXTURE)  # 새 프로세스를 흉내: 메모리 해시 없음
        blocked = user_typed(fresh, "@@ 다른 말")
        self.assertEqual(blocked.status, ChatStatus.NEEDS_DUPLICATE_CONFIRM)
        done = user_typed(fresh, "@@ 다른 말", duplicate_confirmed=True)
        self.assertEqual(done.status, ChatStatus.DONE)
        after = user_typed(self.service(known=KNOWN_FIXTURE), "@@ 또 다른 말")
        self.assertEqual(after.status, ChatStatus.DONE)  # ack 뒤에는 묻지 않는다

    @tag("T17", "T31", "T45")
    def test_refused_origins_never_start_a_process(self):
        self.scenario(stdout=SENT)
        service = self.service()
        judgment = judge_input("@@ 안녕")
        cases = [
            dict(origin=None, provenance=Provenance.TYPED),
            dict(origin=Origin.SYSTEM, provenance=Provenance.TYPED),
            dict(origin=Origin.AI_PREFILL, provenance=Provenance.PREFILLED),
            dict(origin=Origin.AI_APPROVED, provenance=Provenance.TYPED),
            dict(origin=Origin.USER_AT, provenance=Provenance.PREFILLED),
            dict(origin=Origin.USER_AT, provenance=Provenance.PASTED),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(service.send_chat_action(judgment, **case).status, ChatStatus.REFUSED)
        self.assertEqual(self.calls(), [])

    @tag("T33")
    def test_T33_preview_matches_what_is_sent(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": SENT}])
        service = self.service()
        judgment = judge_input("@@ 고마워요")
        plan = service.plan_for(judgment, Origin.USER_AT)
        service.send_chat_action(judgment, origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual(decoded_sent_texts(self.calls()), [plan.text, plan.behaviour])

    def test_ai_path_has_no_auto_emoji_or_behaviour(self):
        from mobinogi_helper.origin import Approval
        import time as _time

        self.scenario(stdout=SENT)
        service = self.service()
        approval = Approval("act1", expires_at=_time.monotonic() + 60)
        result = service.send_chat_action(
            judge_input("@@ 고마워요"), origin=Origin.AI_APPROVED, provenance=Provenance.TYPED,
            approval=approval, action_id="act1",
        )
        self.assertEqual(result.outcome, Outcome.COMPLETED)
        self.assertEqual(decoded_sent_texts(self.calls()), ["고마워요"])

    def test_write_ahead_pending_is_saved_before_the_process_starts(self):
        self.scenario(delay=0.6, stdout=SENT)
        service = self.service()
        thread = threading.Thread(target=lambda: user_typed(service, "@@ 안녕하세요"))
        thread.start()
        self.wait_for_calls(1)  # 프로세스가 이미 시작된 시점에 pending 기록이 있어야 한다
        contexts = self.store().load_all()
        thread.join()
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].steps[0].state, StepState.PENDING)

    def test_store_failure_before_send_blocks_the_send(self):
        from unittest import mock

        self.scenario(stdout=SENT)
        service = self.service()
        with mock.patch("os.replace", side_effect=OSError("disk")):
            result = user_typed(service, "@@ 안녕하세요")
        self.assertEqual(result.status, ChatStatus.STORE_FAILED)
        self.assertEqual(self.calls(), [])

    def test_status_of_input_that_is_not_chat(self):
        self.assertEqual(
            self.service().send_chat_action(judge_input("안녕"), origin=Origin.USER_AT, provenance=Provenance.TYPED).status,
            ChatStatus.INVALID_INPUT,
        )


class ChatPathInjectionTests(FakeCliTestCase):
    """계획 §5-1의 오류 주입을 채팅 경로에서 시험한다. 어떤 경우에도 성공을 추측하지 않고 행동은 실행되지 않는다."""

    def _run(self, scenario, known=KNOWN_FIXTURE):
        self.scenario(**scenario)
        self.log_path.unlink(missing_ok=True)
        # 앞선 시험이 남긴 미확인 UNRESOLVED가 재전송 확인을 요구하므로 확인 처리(문답 뒤)한 상태로 실행한다.
        return user_typed(self.service(known=known), "@@ 고마워요", duplicate_confirmed=True)

    def test_exit_codes_2_3_4_5_with_and_without_body_are_unknown(self):
        for code in (2, 3, 4, 5):
            for stdout in ("", json.dumps({"status": "ok"}), json.dumps({"error": "game_off"})):
                with self.subTest(code=code, stdout=stdout):
                    result = self._run({"exit_code": code, "stdout": stdout})
                    self.assertEqual(result.outcome, Outcome.UNRESOLVED)
                    self.assertEqual(len(self.calls()), 1)

    def test_empty_stdout_with_exit_zero_is_unknown(self):
        result = self._run({"exit_code": 0, "stdout": ""})
        self.assertEqual((result.outcome, result.reason), (Outcome.UNRESOLVED, Reason.EMPTY_RESPONSE))

    def test_invalid_utf8_and_cp949_bodies_are_unknown(self):
        for hexed in ("fffe7b7d", '{"status": "한글"}'.encode("cp949").hex()):
            with self.subTest(hexed=hexed):
                result = self._run({"raw_stdout_hex": hexed})
                self.assertEqual(result.outcome, Outcome.UNRESOLVED)

    def test_large_output_under_and_over_limit(self):
        under = self._run({"stdout": SENT, "huge_output": 1000})
        self.assertNotEqual(under.reason, Reason.OUTPUT_TOO_LARGE)
        self.scenario(stdout=SENT, huge_output=2_000_000)
        self.log_path.unlink(missing_ok=True)
        result = user_typed(self.service(broker=self.broker(send_timeout=5)), "@@ 고마워요", duplicate_confirmed=True)
        self.assertEqual((result.outcome, result.reason), (Outcome.UNRESOLVED, Reason.OUTPUT_TOO_LARGE))

    def test_delayed_exit_after_response_is_still_judged(self):
        result = self._run({"stdout": SENT, "delay": 0.2})
        self.assertEqual(result.outcome, Outcome.COMPLETED)

    def test_keyboard_interrupt_during_send_is_unresolved_and_skips_behaviour(self):
        from unittest import mock

        self.scenario(delay=5, stdout=SENT)
        calls = {"n": 0}
        real_sleep = time.sleep

        def interrupting(seconds):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            real_sleep(seconds)

        service = self.service(broker=self.broker(send_timeout=10))
        with mock.patch("mobinogi_helper.cli_adapter.time.sleep", interrupting):
            result = user_typed(service, "@@ 고마워요")
        self.assertEqual((result.outcome, result.reason), (Outcome.UNRESOLVED, Reason.USER_INTERRUPT))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.store().load_all()[0].steps[0].state, StepState.UNKNOWN)


class FuzzTests(FakeCliTestCase):
    def test_seeded_chat_fuzz_never_confirms_or_repeats(self):
        """난수 시드를 고정한 채팅 경로 퍼징(200회 [후보]). 중복 전송 0건·행동 오실행 0건."""
        rng = random.Random(20260921)
        pieces = [
            "", "{", "[]", "[1,2]", "null", '{"status": 1}', '{"status": "weird"}', '{"status": "ok", "error": "x"}',
            '{"status": "ok", "retryAfterSeconds": 5}', '{"body": {"error": "x"}}', "��", "x" * 3000,
        ]
        empty_known = KnownResponses()
        for iteration in range(200):
            self.scenario(
                stdout=rng.choice(pieces),
                exit_code=rng.choice([0, 1, 2, 3, 4, 5, 9]),
                stderr=rng.choice(["", "err"]),
            )
            self.log_path.unlink(missing_ok=True)
            service = self.service(known=empty_known, store=self.store())
            result = user_typed(service, "@@ 고마워요", duplicate_confirmed=True)
            with self.subTest(iteration=iteration):
                self.assertEqual(result.status, ChatStatus.DONE)
                self.assertNotEqual(result.outcome, Outcome.COMPLETED)  # 출고 설정에서는 성공을 추측하지 않는다
                self.assertEqual(len(self.calls()), 1)  # 채팅 1회, 행동 미실행


if __name__ == "__main__":
    unittest.main()
