"""모의 게임 CLI. 시나리오 JSON(FAKE_CLI_SCENARIO)이 동작을 정한다. 테스트 전용이다.

시나리오 필드: exit_code, stdout, stderr, delay, crash, huge_output, spawn_grandchild,
echo_argv, echo_stderr, raw_stdout_hex, calls(호출 순번별 시나리오 목록).
호출 로그(FAKE_CLI_LOG)에는 argv가 기록된다(검사 대상이 아닌 별도 폴더에 둔다).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    scenario_path = os.environ.get("FAKE_CLI_SCENARIO")
    scenario = {}
    if scenario_path and os.path.isfile(scenario_path):
        with open(scenario_path, "r", encoding="utf-8") as handle:
            scenario = json.load(handle)

    log_path = os.environ.get("FAKE_CLI_LOG")
    index = 0
    if log_path:
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8") as handle:
                index = sum(1 for _ in handle)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv}, ensure_ascii=False) + "\n")

    calls = scenario.get("calls")
    if calls:
        scenario = calls[min(index, len(calls) - 1)]

    if scenario.get("spawn_grandchild"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        pid_file = os.environ.get("FAKE_CLI_GRANDCHILD")
        if pid_file:
            with open(pid_file, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))

    delay = float(scenario.get("delay", 0))
    if delay:
        time.sleep(delay)

    if scenario.get("crash"):
        os._exit(int(scenario.get("exit_code", 1)))

    out = sys.stdout.buffer
    if "raw_stdout_hex" in scenario:
        out.write(bytes.fromhex(scenario["raw_stdout_hex"]))
    else:
        text = scenario.get("stdout", "")
        if scenario.get("echo_argv"):
            text = json.dumps({"status": "echo", "argv": argv}, ensure_ascii=True)
        out.write(text.encode("utf-8"))
    huge = int(scenario.get("huge_output", 0))
    if huge:
        out.write(b"x" * huge)
    out.flush()

    stderr_text = scenario.get("stderr", "")
    if scenario.get("echo_stderr"):
        stderr_text = " ".join(argv)
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()
    return int(scenario.get("exit_code", 0))


if __name__ == "__main__":
    raise SystemExit(main())
