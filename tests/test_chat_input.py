import unittest

from mobinogi_helper.chat_input import (
    CHAT,
    NOT_CHAT,
    REASON_EMPTY,
    REASON_MULTILINE,
    REASON_RESERVED,
    REASON_TOO_LONG,
    REJECTED,
    judge_input,
    parse_chat_input,
)
from mobinogi_helper.chat_plan import build_chat_plan

from .support import tag


class JudgeInputTests(unittest.TestCase):
    @tag("T2")
    def test_T2_leading_whitespace_still_chat(self):
        judgment = judge_input("  @@안녕하세요")
        self.assertEqual(judgment.kind, CHAT)
        self.assertEqual(judgment.message, "안녕하세요")

    @tag("T3")
    def test_T3_fullwidth_prefix_is_normalized(self):
        judgment = judge_input("＠＠안녕")
        self.assertEqual(judgment.kind, CHAT)
        self.assertEqual(judgment.message, "안녕")
        self.assertTrue(judgment.normalized)

    @tag("T4")
    def test_T4_length_boundary(self):
        self.assertEqual(judge_input("@@ " + "가" * 48).kind, CHAT)
        too_long = judge_input("@@ " + "가" * 49)
        self.assertEqual(too_long.kind, REJECTED)
        self.assertEqual(too_long.reason, REASON_TOO_LONG)

    @tag("T5")
    def test_T5_reserved_prefixes_rejected(self):
        for line in ("@@ /지역", "@@ #x", "@@ ／지역", "@@ ＃x", "@@ ​/지역"):
            with self.subTest(line=line):
                judgment = judge_input(line)
                self.assertEqual(judgment.kind, REJECTED)
                self.assertEqual(judgment.reason, REASON_RESERVED)

    @tag("T6")
    def test_T6_multiline_rejected_before_normalization(self):
        judgment = judge_input("@@ 안녕\n@@ 두 번째")
        self.assertEqual(judgment.kind, REJECTED)
        self.assertEqual(judgment.reason, REASON_MULTILINE)

    @tag("T41")
    def test_T41_empty_body(self):
        for line in ("@@", "@@ ", "@@​"):
            with self.subTest(line=line):
                self.assertEqual(judge_input(line).reason, REASON_EMPTY)

    @tag("T42")
    def test_T42_zwj_and_variation_selector_preserved(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        judgment = judge_input(f"@@ {family}")
        self.assertEqual(judgment.message, family)

    def test_plain_text_is_not_chat(self):
        self.assertEqual(judge_input("오늘 뭐 하지?").kind, NOT_CHAT)

    def test_compat_wrapper(self):
        self.assertEqual(parse_chat_input("@@ 안녕하세요"), "안녕하세요")
        self.assertIsNone(parse_chat_input("안녕하세요"))
        with self.assertRaises(ValueError):
            parse_chat_input("@@")


class ChatPlanTests(unittest.TestCase):
    def test_emoji_and_behaviour_for_clear_intent(self):
        plan = build_chat_plan("고마워요")
        self.assertEqual(plan.text, "고마워요 😍")
        self.assertEqual(plan.behaviour, "/하트")

    def test_only_emoji_for_ambiguous_text(self):
        plan = build_chat_plan("길드챗 테스트")
        self.assertEqual(plan.text, "길드챗 테스트 😊")
        self.assertIsNone(plan.behaviour)

    def test_negation_and_objects_do_not_trigger_behaviour(self):
        for text in ("안 고마워", "사과 팔아요", "사과나무 심었어"):
            with self.subTest(text=text):
                self.assertIsNone(build_chat_plan(text).behaviour)

    def test_auto_behaviour_can_be_disabled(self):
        self.assertIsNone(build_chat_plan("고마워요", auto_behaviour=False).behaviour)


if __name__ == "__main__":
    unittest.main()
