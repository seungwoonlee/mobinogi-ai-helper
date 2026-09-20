import unittest

from mabinogi_chat import build_chat_plan, parse_chat_input


class ParseChatInputTests(unittest.TestCase):
    def test_extracts_message_after_prefix(self):
        self.assertEqual(parse_chat_input("@@ 안녕하세요"), "안녕하세요")

    def test_extracts_message_without_space_after_prefix(self):
        self.assertEqual(parse_chat_input("@@안녕하세요"), "안녕하세요")

    def test_ignores_non_command_input(self):
        self.assertIsNone(parse_chat_input("안녕하세요"))

    def test_rejects_empty_message(self):
        with self.assertRaises(ValueError):
            parse_chat_input("@@")

    def test_rejects_reserved_command(self):
        with self.assertRaises(ValueError):
            parse_chat_input("@@ /지역")

    def test_rejects_too_long_message(self):
        with self.assertRaises(ValueError):
            parse_chat_input("@@ " + "가" * 49)

    def test_adds_emoji_and_behaviour_for_clear_intent(self):
        plan = build_chat_plan("고마워요")
        self.assertEqual(plan.text, "고마워요 😍")
        self.assertEqual(plan.behaviour, "/하트")

    def test_adds_only_emoji_for_ambiguous_text(self):
        plan = build_chat_plan("길드챗 테스트")
        self.assertEqual(plan.text, "길드챗 테스트 😊")
        self.assertIsNone(plan.behaviour)


if __name__ == "__main__":
    unittest.main()
