"""구현·테스트 리뷰(1회차)에서 지적된 결함의 회귀 테스트."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from mobinogi_helper.action_context import (
    ActionContext,
    ActionStore,
    OwnerState,
    Outcome,
    Step,
    StepState,
    StoreError,
    new_action_id,
    owner_state,
    process_start_time,
)
from mobinogi_helper.broker import CommandState, QueryKind
from mobinogi_helper.chat_input import REASON_MULTILINE, REJECTED, judge_input
from mobinogi_helper.command_catalog import Kind
from mobinogi_helper.origin import Origin, Provenance
from mobinogi_helper.reasons import Reason
from mobinogi_helper.response_judge import KnownResponses, VerdictKind, judge_write_chat
from mobinogi_helper.cli_adapter import RawResult

from . import FAKE_CLI
from .support import KNOWN_FIXTURE, SENT, FakeCliTestCase, tag


class OwnerProbeTests(FakeCliTestCase):
    """소유 프로세스 판정을 실제 프로세스로 시험한다(주입 없이)."""

    def _spawn(self, delay):
        self.scenario(delay=delay, stdout="{}")
        return subprocess.Popen([sys.executable, str(FAKE_CLI), "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_finished_process_is_dead_not_unknown(self):
        proc = self._spawn(0)
        start = process_start_time(proc.pid)
        proc.wait(timeout=20)
        self.assertEqual(owner_state(proc.pid, start), OwnerState.DEAD)

    def test_running_process_is_alive_and_reused_start_is_dead(self):
        proc = self._spawn(20)
        self.addCleanup(proc.kill)
        start = process_start_time(proc.pid)
        if os.name == "nt":
            self.assertEqual(owner_state(proc.pid, start), OwnerState.ALIVE)
            self.assertEqual(owner_state(proc.pid, (start or 0) + 1), OwnerState.DEAD)  # PID 재사용
            self.assertEqual(owner_state(proc.pid, None), OwnerState.UNKNOWN)  # 시작 시각이 없으면 판정 불가
        else:
            self.assertEqual(owner_state(proc.pid, None), OwnerState.ALIVE)

    def test_real_dead_owner_is_closed_by_close_orphans(self):
        proc = self._spawn(0)
        start = process_start_time(proc.pid)
        proc.wait(timeout=20)
        store = self.store()
        context = ActionContext(
            action_id=new_action_id(), kind=Kind.CHAT, label="게임 채팅 전송", origin="USER_AT", command="write_chat",
            created_at=1.0, owner_pid=proc.pid, owner_start=start, steps=[Step("chat")],
        )
        store.save(context)
        self.assertEqual(store.close_orphans(), {"closed": 1, "undetermined": 0})
        self.assertEqual(store.get(context.action_id).outcome, Outcome.UNRESOLVED)


class AdapterPipeTests(FakeCliTestCase):
    def test_normal_exit_with_grandchild_holding_pipe_returns_quickly(self):
        self.scenario(spawn_grandchild=True, stdout=json.dumps({"status": "x"}))
        os.environ["FAKE_CLI_GRANDCHILD"] = str(self.tmp / "gc.pid")
        started = time.monotonic()
        raw = self.adapter().run("status", timeout=10)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(raw.exit_code, 0)
        self.assertIn('"status"', raw.stdout)
        pid_file = self.tmp / "gc.pid"
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())

        def cleanup():
            import signal

            try:
                os.kill(pid, signal.SIGTERM)  # 손자 정리(Windows에서는 TerminateProcess)
            except OSError:
                pass

        self.addCleanup(cleanup)


class JudgeMarkerTests(unittest.TestCase):
    def _judge(self, payload):
        return judge_write_chat(RawResult(0, stdout=json.dumps(payload)), KNOWN_FIXTURE)

    def test_null_top_level_does_not_hide_body_markers(self):
        for payload in (
            {"status": "ok", "error": None, "body": {"error": "boom"}},
            {"status": "ok", "retryAfterSeconds": None, "body": {"retryAfterSeconds": 3}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._judge(payload).kind, VerdictKind.UNKNOWN)

    def test_reasons_for_process_exit_and_code_5(self):
        empty_5 = judge_write_chat(RawResult(5, stdout=""), KNOWN_FIXTURE)
        self.assertEqual(empty_5.reason, Reason.CODE_5)
        empty_2 = judge_write_chat(RawResult(2, stdout=""), KNOWN_FIXTURE)
        self.assertEqual(empty_2.reason, Reason.PROCESS_EXIT)

    def test_unicode_line_separators_are_rejected_as_multiline(self):
        for mark in (" ", " ", "\x0b", "\x0c", "\x85"):
            with self.subTest(mark=repr(mark)):
                judgment = judge_input(f"@@ 안녕{mark}두번째")
                self.assertEqual((judgment.kind, judgment.reason), (REJECTED, REASON_MULTILINE))


class BrokerRobustnessTests(FakeCliTestCase):
    def test_stale_generation_failure_is_discarded_too(self):
        self.scenario(delay=5, stdout="{}")
        broker = self.broker(query_timeout=1.0)
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault("r", broker.run_query("status")))
        thread.start()
        self.wait_for_calls(1)
        broker.bump_generation()
        thread.join()
        self.assertEqual(holder["r"].kind, QueryKind.STALE)

    def test_unknown_send_notifies_failed_then_idle_with_reason(self):
        self.scenario(stdout=SENT)
        seen = []
        broker = self.broker(send_timeout=5)
        broker.add_observer(lambda a, b, r: seen.append((b, r)))
        service = self.service(known=KnownResponses(), broker=broker)
        service.send_chat_action(judge_input("@@ 안녕하세요"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        states = [state for state, _ in seen]
        self.assertEqual(states, [CommandState.RUNNING_SEND, CommandState.FAILED, CommandState.IDLE])
        self.assertEqual(seen[1][1], Reason.NO_KNOWN_RESPONSES)

    def test_observer_exception_does_not_lose_the_result(self):
        self.scenario(stdout=SENT)
        broker = self.broker(send_timeout=5)
        broker.add_observer(lambda a, b, r: 1 / 0)
        result = self.service(broker=broker).send_chat_action(
            judge_input("@@ 길드챗 테스트"), origin=Origin.USER_AT, provenance=Provenance.TYPED
        )
        self.assertEqual(result.outcome, Outcome.COMPLETED)
        self.assertEqual(broker.state, CommandState.IDLE)

    def test_adapter_exception_does_not_leave_broker_running(self):
        broker = self.broker()
        with mock.patch.object(broker._adapter, "run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                broker.run_query("status")
        self.assertEqual(broker.state, CommandState.IDLE)
        self.assertNotEqual(broker.run_query("status", wait_ms=0).kind, QueryKind.BUSY)


class PersistenceTests(FakeCliTestCase):
    def test_unknown_chat_is_saved_as_unresolved_even_if_final_save_fails(self):
        self.scenario(stdout=SENT)
        store = self.store()
        real_save = store.save
        calls = {"n": 0}

        def flaky(context):
            calls["n"] += 1
            if calls["n"] >= 3:  # pending 저장, 결과 저장 뒤의 최종 저장을 실패시킨다
                raise StoreError("disk")
            real_save(context)

        with mock.patch.object(store, "save", flaky):
            result = self.service(known=KnownResponses(), store=store).send_chat_action(
                judge_input("@@ 안녕하세요"), origin=Origin.USER_AT, provenance=Provenance.TYPED
            )
        self.assertTrue(result.store_warning)
        saved = self.store().load_all()[0]
        self.assertEqual(saved.outcome, Outcome.UNRESOLVED)
        self.assertEqual(saved.steps[0].state, StepState.UNKNOWN)
        self.assertEqual(len(self.store().unacknowledged_unresolved()), 1)

    def test_interrupted_behaviour_resend_never_allows_another_resend(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "rejected"})}, {"delay": 3, "stdout": SENT}])
        service = self.service()
        first = service.send_chat_action(judge_input("@@ 고마워요"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertTrue(first.behaviour_resend_allowed)
        thread = threading.Thread(
            target=lambda: service.resend_behaviour(first.context_id, "/하트", origin=Origin.USER_AT, provenance=Provenance.TYPED)
        )
        thread.start()
        self.wait_for_calls(3)
        during = self.store().get(first.context_id)  # 재전송 도중(pending)의 기록
        self.assertFalse(during.behaviour_resend_allowed)
        self.assertEqual(during.step("behaviour").state, StepState.PENDING)
        thread.join()

        dead_store = self.store(owner_probe=lambda pid, start: OwnerState.DEAD)
        during.owner_pid = 999999
        dead_store.save(during)
        dead_store.close_orphans()
        closed = dead_store.get(first.context_id)
        self.assertEqual(closed.outcome, Outcome.PARTIAL)
        self.assertFalse(closed.behaviour_resend_allowed)

    def test_os_error_while_reading_does_not_move_files_aside(self):
        store = self.store()
        context = ActionContext(
            action_id=new_action_id(), kind=Kind.CHAT, label="게임 채팅 전송", origin="USER_AT", command="write_chat",
            created_at=1.0, owner_pid=os.getpid(), steps=[Step("chat")],
        )
        store.save(context)
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            self.assertEqual(store.load_all(), [])
        self.assertTrue((self.store_dir / f"{context.action_id}.json").exists())
        self.assertFalse(list(self.store_dir.glob("*.corrupt")))


if __name__ == "__main__":
    unittest.main()
