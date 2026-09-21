"""구현·테스트 리뷰(2회차)에서 지적된 결함의 회귀 테스트."""

from mobinogi_helper.action_context import Outcome
from mobinogi_helper.chat_input import judge_input
from mobinogi_helper.chat_service import ChatStatus
from mobinogi_helper.origin import Origin, Provenance
from mobinogi_helper.reasons import Reason

from .support import FakeCliTestCase


class DeeplyNestedResponseTests(FakeCliTestCase):
    """깊게 중첩된 JSON 응답이 RecursionError로 전송 경로를 죽이지 않는지 확인한다(구현 리뷰 2회차 H1)."""

    def test_deeply_nested_json_response_is_unresolved_not_a_crash(self):
        self.scenario(stdout="[" * 5000)
        service = self.service()
        result = service.send_chat_action(judge_input("@@ 안녕하세요"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual(result.status, ChatStatus.DONE)
        self.assertEqual(result.outcome, Outcome.UNRESOLVED)
        self.assertEqual(result.reason, Reason.JSON_ERROR)


class RetryAfterZeroTests(FakeCliTestCase):
    """retryAfterSeconds: 0이 전송 확정으로 오판정되지 않는지 확인한다(테스트 리뷰 2회차 H1)."""

    def test_retry_after_zero_does_not_confirm_send(self):
        self.scenario(stdout='{"status": "ok", "retryAfterSeconds": 0}')
        service = self.service()
        result = service.send_chat_action(judge_input("@@ 안녕하세요"), origin=Origin.USER_AT, provenance=Provenance.TYPED)
        self.assertEqual(result.status, ChatStatus.DONE)
        self.assertEqual(result.outcome, Outcome.UNRESOLVED)
        self.assertEqual(result.reason, Reason.NOT_SUCCESS_MARKER)
