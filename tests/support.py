"""테스트 공용 기반: 모의 CLI 실행, 임시 저장 폴더, 시나리오 주입."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from mobinogi_helper.action_context import ActionStore
from mobinogi_helper.broker import CommandBroker
from mobinogi_helper.chat_service import ChatService
from mobinogi_helper.cli_adapter import CliAdapter
from mobinogi_helper.response_judge import KnownResponses

from . import FAKE_CLI, guard_violations

# 테스트 전용 알려진 응답(출고 설정과 무관한 고정 픽스처).
KNOWN_FIXTURE = KnownResponses(
    config_verified=True,
    sent_statuses=frozenset({"ok"}),
    rejected_statuses=frozenset({"rejected"}),
)
SENT = json.dumps({"status": "ok"})


class FakeCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="mobinogi-test-"))
        self.store_dir = self.tmp / "store"
        self.log_dir = self.tmp / "clilog"  # 저장 폴더와 분리(원문 검사 대상 밖)
        self.log_dir.mkdir()
        self.scenario_path = self.tmp / "scenario.json"
        self.log_path = self.log_dir / "calls.jsonl"
        self._env = dict(os.environ)
        os.environ["FAKE_CLI_SCENARIO"] = str(self.scenario_path)
        os.environ["FAKE_CLI_LOG"] = str(self.log_path)
        os.environ["MOBINOGI_STORE_DIR"] = str(self.store_dir)
        self.addCleanup(self._restore)
        self.before_violations = guard_violations()

    def _restore(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tearDown(self) -> None:
        self.assertEqual(guard_violations(), self.before_violations, "가드 위반")
        super().tearDown()

    # ---- 헬퍼 ---------------------------------------------------------
    def scenario(self, **kwargs) -> None:
        self.scenario_path.write_text(json.dumps(kwargs), encoding="utf-8")

    def adapter(self) -> CliAdapter:
        return CliAdapter(Path(sys.executable), prefix_args=[str(FAKE_CLI)])

    def broker(self, **kwargs) -> CommandBroker:
        return CommandBroker(self.adapter(), **kwargs)

    def store(self, **kwargs) -> ActionStore:
        return ActionStore(self.store_dir, **kwargs)

    def service(self, known=KNOWN_FIXTURE, broker=None, store=None, **kwargs) -> ChatService:
        return ChatService(broker or self.broker(send_timeout=5.0), store or self.store(), known, **kwargs)

    def calls(self) -> list:
        if not self.log_path.is_file():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def stored_text(self) -> str:
        """저장 폴더의 모든 파일 내용을 합친다(원문 검사용)."""
        parts = []
        if self.store_dir.is_dir():
            for path in sorted(self.store_dir.iterdir()):
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)


def tag(*ids: str):
    """추적표 메타 테스트가 읽는 시나리오 ID 태그."""

    def decorator(func):
        func.scenario_ids = ids
        return func

    return decorator
