import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from mobinogi_helper.cli_adapter import (
    CliAdapter,
    CliNotFound,
    DEFAULT_CLI_PATH,
    ENV_CLI,
    adapter_for,
    locate_cli,
)
from mobinogi_helper.reasons import Failure

from . import FAKE_CLI, acknowledge_violations, guard_violations
from .support import FakeCliTestCase, tag


class LocateCliTests(unittest.TestCase):
    """탐색 순서는 실행 없이 is_file만 대체해 시험한다."""

    def test_env_path_wins_and_is_exclusive(self):
        found = locate_cli({ENV_CLI: "C:/x/cli.exe"}, is_file=lambda p: str(p).endswith("cli.exe"))
        self.assertEqual(found, Path("C:/x/cli.exe"))

    def test_env_set_but_missing_does_not_fall_back(self):
        # 기본 경로가 존재해도 환경 변수 경로가 없으면 실패해야 한다.
        with self.assertRaises(CliNotFound):
            locate_cli({ENV_CLI: "C:/missing.exe"}, is_file=lambda p: p == DEFAULT_CLI_PATH)

    def test_default_when_env_unset(self):
        found = locate_cli({}, is_file=lambda p: p == DEFAULT_CLI_PATH)
        self.assertEqual(found, DEFAULT_CLI_PATH)

    def test_not_found_anywhere(self):
        with self.assertRaises(CliNotFound):
            locate_cli({}, is_file=lambda p: False, which=lambda name: None)

    def test_py_cli_uses_interpreter(self):
        adapter = adapter_for(Path("fake.py"))
        self.assertEqual(adapter._executable, Path(sys.executable))


class CliAdapterTests(FakeCliTestCase):
    @tag("C1")
    def test_C1_runs_and_captures(self):
        self.scenario(stdout=json.dumps({"pipe": "connected"}), exit_code=0)
        raw = self.adapter().run("status", timeout=10)
        self.assertEqual(raw.exit_code, 0)
        self.assertIsNone(raw.failure)
        self.assertIn("connected", raw.stdout)

    def test_repr_hides_output(self):
        self.scenario(stdout="SECRET-BODY", exit_code=0)
        raw = self.adapter().run("status", timeout=10)
        self.assertNotIn("SECRET-BODY", repr(raw))

    def test_timeout_kills_and_reports(self):
        self.scenario(delay=5, stdout="{}")
        raw = self.adapter().run("status", timeout=0.3)
        self.assertEqual(raw.failure, Failure.TIMEOUT)

    def test_nonzero_exit_is_a_fact_not_a_verdict(self):
        self.scenario(exit_code=5, stdout="{}")
        raw = self.adapter().run("status", timeout=10)
        self.assertEqual(raw.exit_code, 5)
        self.assertIsNone(raw.failure)

    def test_output_over_limit(self):
        self.scenario(huge_output=200_000, stdout="{}")
        raw = self.adapter().run("status", timeout=10, max_output=50_000)
        self.assertEqual(raw.failure, Failure.OUTPUT_TOO_LARGE)

    def test_output_under_limit_is_fine(self):
        self.scenario(huge_output=1000, stdout="{}")
        raw = self.adapter().run("status", timeout=10, max_output=50_000)
        self.assertIsNone(raw.failure)

    def test_invalid_utf8_is_replaced(self):
        self.scenario(raw_stdout_hex="ff fe 7b 7d".replace(" ", ""))
        raw = self.adapter().run("status", timeout=10)
        self.assertIn("\ufffd", raw.stdout)

    def test_stderr_only(self):
        self.scenario(stderr="boom", stdout="", exit_code=2)
        raw = self.adapter().run("status", timeout=10)
        self.assertEqual(raw.stdout, "")
        self.assertEqual(raw.stderr, "boom")

    def test_grandchild_holding_pipe_does_not_hang_and_is_killed(self):
        import os
        import time
        from mobinogi_helper.action_context import process_start_time

        pid_file = self.tmp / "grandchild.pid"
        os.environ["FAKE_CLI_GRANDCHILD"] = str(pid_file)
        self.scenario(spawn_grandchild=True, stdout="{}", delay=5)
        raw = self.adapter().run("status", timeout=1.5)
        self.assertEqual(raw.failure, Failure.TIMEOUT)
        self.assertLess(raw.duration, 20)
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        alive = True
        while time.monotonic() < deadline:
            if os.name != "nt" or process_start_time(pid) is None:
                alive = False
                break
            time.sleep(0.1)
        if os.name == "nt":
            self.assertFalse(alive, "손자 프로세스가 정리되지 않았어요")

    def test_keyboard_interrupt_is_reported_as_interrupted(self):
        self.scenario(delay=5, stdout="{}")
        calls = {"n": 0}
        real_sleep = __import__("time").sleep

        def interrupting_sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            real_sleep(seconds)

        with mock.patch("mobinogi_helper.cli_adapter.time.sleep", interrupting_sleep):
            raw = self.adapter().run("status", timeout=10)
        self.assertEqual(raw.failure, Failure.INTERRUPTED)

    def test_cp949_child_output_is_not_a_crash(self):
        self.scenario(raw_stdout_hex='{"status": "한글"}'.encode("cp949").hex())
        raw = self.adapter().run("status", timeout=10)
        self.assertIsNone(raw.failure)  # 복호화 실패는 치환되고 판정은 JSON 오류·불명으로 이어진다

    def test_missing_executable_is_launch_failure_without_argv(self):
        adapter = CliAdapter(Path(sys.executable).with_name("no-such-cli.exe"))
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError("SECRETARGV")):
            raw = adapter.run("write_chat", ["base64:SECRETARGV"], timeout=1)
        self.assertEqual(raw.failure, Failure.LAUNCH_FAILED)
        self.assertNotIn("SECRETARGV", repr(raw))

    def test_permission_denied_is_launch_failure(self):
        with mock.patch("subprocess.Popen", side_effect=PermissionError("denied")):
            raw = self.adapter().run("status", timeout=1)
        self.assertEqual(raw.failure, Failure.LAUNCH_FAILED)

    def test_cancel_event_stops_process(self):
        import threading

        self.scenario(delay=5, stdout="{}")
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        raw = self.adapter().run("status", timeout=10, cancel_event=cancel)
        self.assertEqual(raw.failure, Failure.INTERRUPTED)


class EnvironmentGuardTests(FakeCliTestCase):
    def test_cli_env_var_is_pinned_to_fake_cli(self):
        self.assertEqual(locate_cli(), FAKE_CLI)
        self.assertEqual(adapter_for(locate_cli())._executable, Path(sys.executable))

    def test_default_cli_path_is_never_selected_in_tests(self):
        self.assertNotEqual(locate_cli(), DEFAULT_CLI_PATH)


class GuardTests(FakeCliTestCase):
    def test_low_level_create_process_guard(self):
        try:
            import _winapi  # type: ignore
        except ImportError:
            self.skipTest("Windows 전용")
        from . import GuardViolation

        denied = [
            f'"{DEFAULT_CLI_PATH}" write_chat base64:AA==',
            f'cmd.exe /c "{FAKE_CLI}"',  # 허용 경로 문자열이 들어 있어도 실행 파일이 다르면 거부
            r"C:\Windows\System32	askkill.exe /IM MabinogiMobile_CLI.exe /F",
            r"C:\Windows\System32	askkill.exe /PID 4 /T /F",  # 등록되지 않은 PID
        ]
        for command in denied:
            with self.subTest(command=command):
                with self.assertRaises(GuardViolation):
                    _winapi.CreateProcess(None, command, None, None, False, 0, None, None, None)
                acknowledge_violations(1)

    def test_guard_blocks_real_cli_and_counts(self):
        from . import GuardViolation
        import subprocess

        before = guard_violations()
        with self.assertRaises(GuardViolation):
            subprocess.Popen([str(DEFAULT_CLI_PATH), "write_chat", "base64:AA=="])
        self.assertEqual(guard_violations(), before + 1)
        acknowledge_violations(1)  # 의도적으로 낸 위반은 기대치에서 제외한다

    def test_guard_blocks_os_system(self):
        from . import GuardViolation

        before = guard_violations()
        with self.assertRaises(GuardViolation):
            os.system("echo blocked")
        self.assertEqual(guard_violations(), before + 1)
        acknowledge_violations(1)

    def test_fake_cli_path_is_allowed(self):
        self.scenario(stdout="{}")
        self.assertIsNone(self.adapter().run("status", timeout=10).failure)


if __name__ == "__main__":
    unittest.main()
