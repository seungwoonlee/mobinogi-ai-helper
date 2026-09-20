import io
import json
import sys
import unittest
from unittest import mock

import mabinogi_chat
from mabinogi_chat import Tool, build_chat_plan, main, parse_chat_input, safe_print
from mobinogi_helper.response_judge import KnownResponses

from .support import KNOWN_FIXTURE, SENT, FakeCliTestCase, tag

EMPTY = KnownResponses()


class Answers:
    """확인 질문에 미리 정한 답을 준다."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""


class ToolTests(FakeCliTestCase):
    def tool(self, answers, known=KNOWN_FIXTURE):
        self.out = io.StringIO()
        return Tool(self.service(known=known), ask=answers, out=self.out)

    def test_compat_parse_and_plan_still_work(self):
        self.assertEqual(parse_chat_input("@@ 안녕하세요"), "안녕하세요")
        self.assertEqual(build_chat_plan("고마워요").behaviour, "/하트")

    @tag("T1")
    def test_T1_success_path_and_exit_code_0(self):
        self.scenario(stdout=SENT)
        tool = self.tool(Answers("y"))
        self.assertEqual(tool.handle_line("@@ 길드챗 테스트"), 0)
        self.assertIn("전송 완료", self.out.getvalue())

    def test_preview_is_shown_before_asking(self):
        self.scenario(stdout=SENT)
        answers = Answers("n")
        tool = self.tool(answers)
        self.assertEqual(tool.handle_line("@@ 고마워요"), 2)
        text = self.out.getvalue()
        self.assertIn("보낼 말: 고마워요 😍 (행동: /하트)", text)
        self.assertIn("다른 플레이어에게 보이는 게임 채팅", text)
        self.assertEqual(self.calls(), [])  # 취소하면 아무것도 보내지 않는다

    def test_only_exact_y_confirms(self):
        self.scenario(stdout=SENT)
        for answer in ("", "yes", "ｙ", "ㅛ", "예", "n", "y y"):
            with self.subTest(answer=answer):
                tool = self.tool(Answers(answer))
                self.assertEqual(tool.handle_line("@@ 안녕하세요"), 2)
        self.assertEqual(self.calls(), [])

    @tag("T10")
    def test_shipped_empty_config_says_unconfirmed_with_reason_and_exit_3(self):
        self.scenario(stdout=SENT)
        tool = self.tool(Answers("y"), known=EMPTY)
        self.assertEqual(tool.handle_line("@@ 길드챗 테스트"), 3)
        text = self.out.getvalue()
        self.assertIn("전송됐을 수 있지만", text)
        self.assertIn("표본 미확정", text)
        self.assertNotIn("전송 완료", text)

    def test_second_send_after_unresolved_asks_question_2_and_ack_releases(self):
        self.scenario(stdout=SENT)
        first = self.tool(Answers("y"), known=EMPTY)
        first.handle_line("@@ 길드챗 테스트")
        answers = Answers("n")
        second = Tool(self.service(known=KNOWN_FIXTURE), ask=answers, out=io.StringIO())
        self.assertEqual(second.handle_line("@@ 다른 말"), 7)
        self.assertIn("채팅창에서 확인했나요", answers.prompts[0])
        self.assertEqual(len(self.calls()), 1)
        answers = Answers("y", "y")
        third = Tool(self.service(known=KNOWN_FIXTURE), ask=answers, out=io.StringIO())
        self.assertEqual(third.handle_line("@@ 다른 말"), 0)
        self.assertEqual(len(answers.prompts), 2)  # 질문 2 다음에 질문 1
        answers = Answers("y")
        fourth = Tool(self.service(known=KNOWN_FIXTURE), ask=answers, out=io.StringIO())
        self.assertEqual(fourth.handle_line("@@ 또 다른 말"), 0)
        self.assertEqual(len(answers.prompts), 1)  # 확인한 뒤에는 질문 2가 없다

    def test_exit_codes(self):
        self.scenario(calls=[{"stdout": SENT}, {"stdout": json.dumps({"status": "rejected"})}])
        self.assertEqual(self.tool(Answers("y")).handle_line("@@ 고마워요"), 5)
        self.scenario(stdout=json.dumps({"status": "rejected"}))
        self.assertEqual(self.tool(Answers("y")).handle_line("@@ 안녕하세요"), 4)

    @tag("T5")
    def test_rejections_use_exit_code_2_without_process(self):
        tool = self.tool(Answers())
        for line in ("@@ /지역", "@@", "@@ " + "가" * 49, "일반 대화"):
            with self.subTest(line=line):
                self.assertEqual(tool.handle_line(line), 2)
        self.assertEqual(self.calls(), [])

    def test_stdout_never_contains_cli_output_or_argv(self):
        self.scenario(stdout=json.dumps({"status": "weird", "secret": "SECRET-CLI-BODY"}), echo_stderr=True, stderr="x")
        tool = self.tool(Answers("y"))
        tool.handle_line("@@ 카나리문구")
        self.assertNotIn("SECRET-CLI-BODY", self.out.getvalue())
        self.assertNotIn("base64:", self.out.getvalue())


class MainTests(FakeCliTestCase):
    def test_non_tty_is_refused_without_any_process(self):
        with mock.patch.object(mabinogi_chat, "_stdin_is_terminal", return_value=False):
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(main(["@@ 안녕하세요"]), 6)
        self.assertEqual(self.calls(), [])

    def test_missing_cli_fails_cleanly(self):
        import os

        os.environ["MABINOGI_MOBILE_CLI"] = str(self.tmp / "no-such-cli.exe")
        with mock.patch.object(mabinogi_chat, "_stdin_is_terminal", return_value=True):
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(main(["@@ 안녕하세요"]), 1)
        self.assertEqual(self.calls(), [])


class BuildToolTests(FakeCliTestCase):
    def test_corrupt_known_responses_warns_and_everything_stays_unresolved(self):
        import os

        bad = self.tmp / "known.json"
        bad.write_text("{", encoding="utf-8")
        os.environ["MOBINOGI_KNOWN_RESPONSES"] = str(bad)
        self.scenario(stdout=SENT)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", new=stderr):
            tool = mabinogi_chat.build_tool()
        self.assertIsNotNone(tool)
        self.assertIn("확인 불가", stderr.getvalue())
        tool._out = io.StringIO()
        tool._ask = Answers("y")
        self.assertEqual(tool.handle_line("@@ 길드챗 테스트"), 3)

    def test_keyboard_interrupt_at_the_prompt_exits_cleanly(self):
        self.scenario(stdout=SENT)
        tool = Tool(self.service(), ask=Answers(), out=io.StringIO())
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(tool.run_interactive(), 0)
        self.assertEqual(self.calls(), [])


class SafePrintTests(unittest.TestCase):
    def test_cp949_stream_does_not_raise_on_emoji(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp949", errors="strict", write_through=True)
        safe_print("전송 완료: 안녕 😊", stream)
        stream.flush()
        self.assertIn("전송 완료".encode("cp949"), raw.getvalue())


if __name__ == "__main__":
    unittest.main()
