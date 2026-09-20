"""게임 CLI 탐색과 실행. 원시 결과(사실)만 돌려주고 의미는 판정하지 않는다.

argv(채팅 문구의 Base64 본문 포함)는 기록하지 않으며 예외 문자열에도 남기지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional, Sequence

from .reasons import Failure

DEFAULT_CLI_PATH = Path(r"C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe")
ENV_CLI = "MABINOGI_MOBILE_CLI"
MAX_OUTPUT_BYTES = 1024 * 1024  # [후보]
_JOIN_SECONDS = 2.0


class CliNotFound(Exception):
    """CLI를 찾지 못했다. 환경 변수가 설정돼 있으면 다른 후보로 넘어가지 않는다."""

    def __init__(self, source: str) -> None:
        super().__init__(f"게임 CLI를 찾지 못했습니다 ({source})")
        self.source = source


@dataclass
class RawResult:
    """CLI 실행의 원시 결과. stdout·stderr 원문은 repr에 나오지 않는다."""

    exit_code: Optional[int]
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    duration: float = 0.0
    failure: Optional[Failure] = None

    def __repr__(self) -> str:  # 원문 노출 방지
        return (
            f"RawResult(exit_code={self.exit_code!r}, duration={self.duration:.3f}, "
            f"failure={self.failure!r}, stdout_len={len(self.stdout)}, stderr_len={len(self.stderr)})"
        )


def locate_cli(
    environ: Optional[dict] = None,
    is_file: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Path:
    """환경 변수 → 기본 경로 → PATH 순으로 CLI를 찾는다.

    환경 변수가 설정돼 있으면 그 경로만 쓰며, 없으면 다른 후보로 넘어가지 않고 실패한다.
    """
    env = os.environ if environ is None else environ
    configured = env.get(ENV_CLI)
    if configured:
        path = Path(configured)
        if is_file(path):
            return path
        raise CliNotFound("환경 변수 경로")

    if is_file(DEFAULT_CLI_PATH):
        return DEFAULT_CLI_PATH

    found = which("MabinogiMobile_CLI")
    if found and Path(found).is_absolute() and is_file(Path(found)):
        return Path(found)
    raise CliNotFound("기본 경로와 PATH")


def adapter_for(path: Path) -> "CliAdapter":
    """경로에 맞는 어댑터를 만든다. `.py` 스크립트(모의 CLI)는 현재 인터프리터로 실행한다."""
    if path.suffix.lower() == ".py":
        return CliAdapter(Path(sys.executable), prefix_args=[str(path)])
    return CliAdapter(path)


def _taskkill_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "taskkill.exe")


class CliAdapter:
    def __init__(self, executable: Path, prefix_args: Sequence[str] = ()) -> None:
        self._executable = Path(executable)
        self._prefix = list(prefix_args)

    def run(
        self,
        command: str,
        args: Sequence[str] = (),
        timeout: Optional[float] = None,
        max_output: int = MAX_OUTPUT_BYTES,
        cancel_event: Optional[threading.Event] = None,
    ) -> RawResult:
        started = time.monotonic()
        argv = [str(self._executable), *self._prefix, command, *args]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        launch_error = False
        proc = None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError:
            launch_error = True
        if launch_error or proc is None:
            # except 블록 밖: 원인 예외(argv 포함 가능)가 __context__로 남지 않는다.
            return RawResult(None, duration=time.monotonic() - started, failure=Failure.LAUNCH_FAILED)

        buffers = {"out": bytearray(), "err": bytearray()}
        exceeded = threading.Event()

        def pump(stream, key: str) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buffers[key].extend(chunk)
                    if len(buffers["out"]) + len(buffers["err"]) > max_output:
                        exceeded.set()
                        break
            except (OSError, ValueError):
                pass

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
        ]
        for thread in threads:
            thread.start()

        failure: Optional[Failure] = None
        deadline = None if timeout is None else started + timeout
        try:
            while True:
                if proc.poll() is not None:
                    break
                if exceeded.is_set():
                    failure = Failure.OUTPUT_TOO_LARGE
                    break
                if cancel_event is not None and cancel_event.is_set():
                    failure = Failure.INTERRUPTED
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    failure = Failure.TIMEOUT
                    break
                time.sleep(0.01)
        except KeyboardInterrupt:
            failure = Failure.INTERRUPTED

        if failure is not None:
            self._kill_tree(proc)
        for thread in threads:
            thread.join(_JOIN_SECONDS)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

        if failure is None and exceeded.is_set():
            failure = Failure.OUTPUT_TOO_LARGE
        exit_code = proc.poll()
        return RawResult(
            exit_code=exit_code if failure is None else exit_code,
            stdout=bytes(buffers["out"]).decode("utf-8", errors="replace"),
            stderr=bytes(buffers["err"]).decode("utf-8", errors="replace"),
            duration=time.monotonic() - started,
            failure=failure,
        )

    @staticmethod
    def _kill_tree(proc: "subprocess.Popen") -> None:
        """종료 여부를 확인한 뒤에만 프로세스 트리를 정리한다."""
        if proc.poll() is not None:
            return
        if os.name == "nt":
            killed = False
            try:
                result = subprocess.run(
                    [_taskkill_path(), "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                killed = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                killed = False
            if not killed:
                _safe_kill(proc)
        else:
            _safe_kill(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _safe_kill(proc: "subprocess.Popen") -> None:
    try:
        proc.kill()
    except OSError:
        pass
