import json
from pathlib import Path
import tempfile
import unittest

from mobinogi_helper.cli_adapter import RawResult
from mobinogi_helper.command_catalog import Kind, classify_command, is_allowed_behaviour, requires_confirm
from mobinogi_helper.reasons import Failure, Reason
from mobinogi_helper.response_judge import (
    KnownResponses,
    VerdictKind,
    judge_write_chat,
    load_known_responses,
)

from .support import KNOWN_FIXTURE, tag

EMPTY = KnownResponses()


def raw(stdout="", exit_code=0, failure=None):
    return RawResult(exit_code=exit_code, stdout=stdout, failure=failure)


def body(**kwargs):
    return json.dumps(kwargs)


class JudgeTests(unittest.TestCase):
    @tag("T1")
    def test_T1_known_success_is_sent(self):
        verdict = judge_write_chat(raw(body(status="ok")), KNOWN_FIXTURE)
        self.assertEqual(verdict.kind, VerdictKind.SENT)

    @tag("T9")
    def test_T9_status_outside_known_is_unknown(self):
        verdict = judge_write_chat(raw(body(status="weird")), KNOWN_FIXTURE)
        self.assertEqual(verdict.kind, VerdictKind.UNKNOWN)
        self.assertEqual(verdict.reason, Reason.UNKNOWN_STATUS)

    @tag("T10")
    def test_T10_json_array_is_protocol_error_and_unknown(self):
        verdict = judge_write_chat(raw("[1,2]"), KNOWN_FIXTURE)
        self.assertEqual(verdict.kind, VerdictKind.UNKNOWN)
        self.assertEqual(verdict.reason, Reason.NOT_OBJECT)
        self.assertTrue(verdict.reason.is_protocol_error)

    @tag("T40")
    def test_T40_shipped_empty_config_makes_everything_unknown(self):
        verdict = judge_write_chat(raw(body(status="ok")), EMPTY)
        self.assertEqual(verdict.kind, VerdictKind.UNKNOWN)
        self.assertEqual(verdict.reason, Reason.NO_KNOWN_RESPONSES)

    @tag("T44")
    def test_T44_rejected_is_not_sent_only_when_verified(self):
        self.assertEqual(
            judge_write_chat(raw(body(status="rejected")), KNOWN_FIXTURE).kind, VerdictKind.NOT_SENT
        )
        unverified = KnownResponses(False, frozenset({"ok"}), frozenset({"rejected"}))
        self.assertEqual(
            judge_write_chat(raw(body(status="rejected")), unverified).kind, VerdictKind.UNKNOWN
        )

    def test_unverified_config_ignores_lists(self):
        unverified = KnownResponses(False, frozenset({"ok"}), frozenset())
        self.assertEqual(judge_write_chat(raw(body(status="ok")), unverified).kind, VerdictKind.UNKNOWN)

    def test_markers_block_success(self):
        cases = [
            body(status="ok", error="x"),
            body(status="ok", retryAfterSeconds=3),
            body(status="ok", body={"error": "x"}),
            body(status="blocked"),
            body(status="timeout"),
            body(status="canceled"),
        ]
        known = KnownResponses(True, frozenset({"ok", "blocked", "timeout", "canceled"}), frozenset())
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(judge_write_chat(raw(text), known).kind, VerdictKind.UNKNOWN)

    def test_retry_after_is_reported_not_retried(self):
        verdict = judge_write_chat(raw(body(status="ok", retryAfterSeconds=7)), KNOWN_FIXTURE)
        self.assertEqual(verdict.retry_after, 7.0)
        self.assertEqual(verdict.kind, VerdictKind.UNKNOWN)

    def test_non_string_status_is_unknown_without_type_error(self):
        for value in (["ok"], {"a": 1}, None, 3):
            with self.subTest(value=value):
                verdict = judge_write_chat(raw(body(status=value)), KNOWN_FIXTURE)
                self.assertEqual(verdict.kind, VerdictKind.UNKNOWN)
                self.assertEqual(verdict.reason, Reason.NOT_STRING_STATUS)

    def test_status_matching_is_exact(self):
        for value in ("OK", " ok", "ok "):
            with self.subTest(value=value):
                self.assertEqual(judge_write_chat(raw(body(status=value)), KNOWN_FIXTURE).kind, VerdictKind.UNKNOWN)

    def test_nonzero_exit_never_sent(self):
        self.assertEqual(judge_write_chat(raw(body(status="ok"), exit_code=5), KNOWN_FIXTURE).kind, VerdictKind.UNKNOWN)

    def test_failures_map_to_reasons(self):
        table = {
            Failure.TIMEOUT: Reason.TIMEOUT,
            Failure.LAUNCH_FAILED: Reason.LAUNCH_FAILED,
            Failure.OUTPUT_TOO_LARGE: Reason.OUTPUT_TOO_LARGE,
            Failure.INTERRUPTED: Reason.USER_INTERRUPT,
        }
        for failure, reason in table.items():
            with self.subTest(failure=failure):
                verdict = judge_write_chat(raw("", None, failure), KNOWN_FIXTURE)
                self.assertEqual((verdict.kind, verdict.reason), (VerdictKind.UNKNOWN, reason))

    def test_empty_and_broken_json(self):
        self.assertEqual(judge_write_chat(raw(""), KNOWN_FIXTURE).reason, Reason.EMPTY_RESPONSE)
        self.assertEqual(judge_write_chat(raw("{"), KNOWN_FIXTURE).reason, Reason.JSON_ERROR)


class ConfigTests(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "known.json"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            return load_known_responses(path)

    def test_valid_file(self):
        known, problem = self._load(json.dumps({"config_verified": True, "sent_statuses": ["ok"], "rejected_statuses": []}))
        self.assertIsNone(problem)
        self.assertTrue(known.usable)

    def test_missing_corrupt_and_wrong_types_fail_closed(self):
        cases = [
            None,
            "{",
            "[]",
            json.dumps({"config_verified": "yes", "sent_statuses": []}),
            json.dumps({"config_verified": True, "sent_statuses": "ok"}),
            json.dumps({"config_verified": True, "sent_statuses": [1]}),
        ]
        for text in cases:
            with self.subTest(text=text):
                known, problem = self._load(text)
                self.assertFalse(known.usable)
                self.assertIsNotNone(problem)

    def test_shipped_config_is_empty_and_unverified(self):
        known, problem = load_known_responses()
        self.assertIsNone(problem)
        self.assertFalse(known.usable)  # 출고 상태: 사용자가 표본을 채우기 전까지 비어 있다


class CatalogTests(unittest.TestCase):
    @tag("T18")
    def test_T18_requires_confirm_normalization(self):
        for value in (True, "true", "True", None, "unknown", 1, ""):
            with self.subTest(value=value):
                self.assertTrue(requires_confirm(value))
        for value in (False, "false", " FALSE "):
            with self.subTest(value=value):
                self.assertFalse(requires_confirm(value))

    def test_unknown_commands_are_actions(self):
        self.assertEqual(classify_command("write_chat"), Kind.CHAT)
        self.assertEqual(classify_command("status"), Kind.QUERY)
        self.assertEqual(classify_command("gather_everything"), Kind.ACTION)

    @tag("T34")
    def test_T34_behaviour_allowlist(self):
        self.assertTrue(is_allowed_behaviour("/하트"))
        self.assertFalse(is_allowed_behaviour("/지역"))
        self.assertFalse(is_allowed_behaviour("하트"))


if __name__ == "__main__":
    unittest.main()
