"""테스트 가드: 실제 게임 CLI가 한 번도 실행되지 않도록 프로세스 생성을 허용 규칙으로 제한한다.

- 허용: 실행 파일 = 현재 인터프리터이고 첫 인자 = 모의 CLI 스크립트, 그리고 등록된 PID를 정리하는 taskkill.
- 위반은 `GuardViolation`(BaseException 계열)으로 던져 넓은 `except Exception`에 삼켜지지 않게 하고,
  위반 횟수를 세어 모든 테스트의 종료 시점에 0인지 검사한다(unittest.TestCase.run 패치).
패키지 로드 시 설치되므로 이 패키지의 모든 테스트 모듈에 적용된다.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"


class GuardViolation(BaseException):
    """허용되지 않은 프로세스 생성 시도."""


class _State:
    violations = 0
    pids: set = set()
    lock = threading.Lock()


def _norm(value) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(value)))
    except (TypeError, ValueError):
        return ""


def _violate(what: str):
    with _State.lock:
        _State.violations += 1
    raise GuardViolation(f"허용되지 않은 프로세스 생성: {what}")


def _allowed_argv(argv) -> bool:
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return False
    first, second = _norm(argv[0]), _norm(argv[1])
    if first == _norm(sys.executable) and second == _norm(FAKE_CLI):
        return True
    if os.path.basename(first) == "taskkill.exe" and "/PID" in argv and "/IM" not in argv:
        try:
            pid = int(argv[list(argv).index("/PID") + 1])
        except (ValueError, IndexError):
            return False
        return pid in _State.pids
    return False


_RealPopen = subprocess.Popen


class _GuardedPopen(_RealPopen):
    def __init__(self, args, *a, **k):
        if not _allowed_argv(args):
            _violate("Popen")
        super().__init__(args, *a, **k)
        with _State.lock:
            _State.pids.add(self.pid)


def _install() -> None:
    subprocess.Popen = _GuardedPopen  # type: ignore[misc]

    def deny(name):
        def guarded(*a, **k):
            _violate(name)
        return guarded

    for name in ("system", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            setattr(os, name, deny(f"os.{name}"))

    try:  # 가장 낮은 관문
        import _winapi  # type: ignore

        real_create = _winapi.CreateProcess

        def create_process(app, cmdline, *a, **k):
            text = (cmdline or "") if isinstance(cmdline, str) else ""
            if str(FAKE_CLI) not in text and "taskkill" not in text.lower():
                _violate("_winapi.CreateProcess")
            return real_create(app, cmdline, *a, **k)

        _winapi.CreateProcess = create_process
    except ImportError:
        pass

    previous_hook = threading.excepthook

    def hook(args):
        if isinstance(args.exc_value, GuardViolation):
            return
        previous_hook(args)

    threading.excepthook = hook

    original_run = unittest.TestCase.run

    def run(self, result=None):
        before = _State.violations
        outcome = original_run(self, result)
        if result is not None and _State.violations > before:
            try:
                raise AssertionError("가드 위반: 허용되지 않은 프로세스 생성이 시도됐어요")
            except AssertionError:
                result.addFailure(self, sys.exc_info())
        return outcome

    unittest.TestCase.run = run  # type: ignore[assignment]


_install()


def guard_violations() -> int:
    return _State.violations


def acknowledge_violations(count: int) -> None:
    """가드 자체를 시험하는 테스트가 의도적으로 낸 위반을 기대치에서 뺀다."""
    with _State.lock:
        _State.violations -= count
