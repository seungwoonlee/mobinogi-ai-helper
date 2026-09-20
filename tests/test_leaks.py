"""채팅 문구·argv·stdout 원문이 로그·저장 파일·출력·예외 표현에 남지 않는지 검사한다(05 §5-3)."""

import base64
import io
import json
import logging
import traceback
import unittest
import urllib.parse

from mabinogi_chat import Tool
from mobinogi_helper.chat_input import judge_input
from mobinogi_helper.origin import Origin, Provenance
from mobinogi_helper.response_judge import KnownResponses

from .support import KNOWN_FIXTURE, FakeCliTestCase, tag

CANARY = "카나리문구입니다XYZ"
BODY_CANARY = "응답원문카나리QRS"


SENT_TEXT = CANARY + " 😊"  # 실제 전송 문자열(정규화·이모지가 붙은 값)


def variants(text: str) -> list:
    """카나리의 여러 표현(문자열 변형과 바이트 변형)을 만든다. 실제 전송 문자열의 변형도 포함한다."""
    if text == CANARY:
        s1, b1 = _variants(text)
        s2, b2 = _variants(SENT_TEXT)
        return list(set(s1) | set(s2)), list({*b1, *b2})
    return _variants(text)


def _variants(text: str) -> list:
    raw = text.encode("utf-8")
    std = base64.b64encode(raw).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
    strings = {
        text,
        std,
        std.rstrip("="),
        urlsafe,
        urlsafe.rstrip("="),
        json.dumps(text, ensure_ascii=True).strip('"'),  # \uXXXX 이스케이프
        urllib.parse.quote(text),
        text[:6],
        std[:6],
    }
    blobs = {text.encode("utf-16le"), text.encode("utf-16be"), text.encode("cp949", errors="ignore"), raw}
    blobs.discard(b"")
    return [s for s in strings if s], list(blobs)


class LeakTests(FakeCliTestCase):
    def _assert_clean(self, haystack: str, label: str, allow_plain: bool = False) -> None:
        strings, _ = variants(CANARY)
        for needle in strings:
            if allow_plain and needle in (CANARY, CANARY[:6], SENT_TEXT):
                continue
            self.assertNotIn(needle, haystack, f"{label}에 카나리({needle!r})가 남아 있어요")
        for needle in (BODY_CANARY, BODY_CANARY[:6]):
            self.assertNotIn(needle, haystack, f"{label}에 응답 원문이 남아 있어요")

    def _run(self, known):
        # 모의 CLI가 argv와 stdout·stderr로 카나리를 에코한다(호출 로그는 별도 폴더에 있어 검사에서 제외).
        self.scenario(
            stdout=json.dumps({"status": "weird", "leak": BODY_CANARY}),
            echo_stderr=True,
            exit_code=3,
        )
        stream = io.StringIO()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                parts = [self.format(record), repr(record.args), repr(record.msg)]
                if record.exc_info:
                    parts.append("".join(traceback.format_exception(*record.exc_info)))
                records.append("\n".join(parts))

        handler = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous_level)

        tool = Tool(self.service(known=known), ask=lambda prompt: "y", out=stream)
        code = tool.handle_line(f"@@ {CANARY}")
        return code, stream.getvalue(), "\n".join(records)

    @tag("T27")
    def test_T27_no_original_text_in_store_output_or_logs(self):
        for known in (KnownResponses(), KNOWN_FIXTURE):
            with self.subTest(known=known.usable):
                code, printed, logs = self._run(known)
                self.assertEqual(code, 3)
                # 사용자가 직접 입력한 문구는 미리보기 줄에만 다시 보인다(전송 전 확인용). 그 밖의 줄과
                # 모든 변형(Base64·이스케이프 등), 응답 원문은 나오면 안 된다.
                self._assert_clean(printed, "표준 출력", allow_plain=True)
                for line in printed.splitlines():
                    if CANARY in line:
                        self.assertTrue(line.startswith("보낼 말:"), f"미리보기 밖에 문구가 있어요: {line!r}")
                self._assert_clean(logs, "로그 레코드")
                self._assert_clean(self.stored_text(), "저장 파일")
                _, blobs = variants(CANARY)
                for path in self.store_dir.iterdir():
                    data = path.read_bytes()
                    for blob in blobs:
                        self.assertNotIn(blob, data)

    def test_exceptions_from_adapter_do_not_carry_argv(self):
        from unittest import mock

        payload = "base64:" + base64.b64encode(CANARY.encode()).decode()
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError(payload)):
            raw = self.adapter().run("write_chat", [payload], timeout=1)
        self._assert_clean(repr(raw) + str(raw.failure), "RawResult")

    def test_service_path_exceptions_and_streams_stay_clean(self):
        """서비스·브로커 경로에서 실행 실패가 나도 예외 표현·표준 출력·오류에 원문이 없어야 한다."""
        import contextlib
        from unittest import mock

        payload = "base64:" + base64.b64encode(SENT_TEXT.encode()).decode()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("subprocess.Popen", side_effect=OSError(payload)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                service = self.service()
                result = service.send_chat_action(
                    judge_input(f"@@ {CANARY}"), origin=Origin.USER_AT, provenance=Provenance.TYPED
                )
        text = repr(result) + str(result.chat_verdict) + out.getvalue() + err.getvalue()
        self._assert_clean(text, "서비스 경로")
        self._assert_clean(self.stored_text(), "저장 파일")

    def test_label_never_contains_chat_text(self):
        self.scenario(stdout=json.dumps({"status": "ok"}))
        from mobinogi_helper.chat_service import ChatStatus

        service = self.service()
        result = service.send_chat_action(judge_input(f"@@ {CANARY}"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual(result.status, ChatStatus.DONE)
        context = self.store().get(result.context_id)
        self._assert_clean(context.label + json.dumps(context.to_dict(), ensure_ascii=False), "컨텍스트")

    def test_result_reprs_do_not_hold_text(self):
        self.scenario(stdout=json.dumps({"status": "ok", "leak": BODY_CANARY}))
        service = self.service()
        result = service.send_chat_action(judge_input(f"@@ {CANARY}"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self._assert_clean(repr(result), "ChatResult")


if __name__ == "__main__":
    unittest.main()
