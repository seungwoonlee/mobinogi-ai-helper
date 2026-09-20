import os
import unittest
from unittest import mock

from mobinogi_helper.action_context import (
    ActionContext,
    ActionStore,
    OwnerState,
    Outcome,
    Step,
    StepState,
    StoreError,
    new_action_id,
)
from mobinogi_helper.command_catalog import Kind
from mobinogi_helper.origin import Approval, Origin, Provenance, check_send_allowed

from .support import FakeCliTestCase, tag


def allowed(origin, provenance=Provenance.TYPED, kind=Kind.CHAT, **kw):
    return check_send_allowed(origin, provenance, kind, **kw).allowed


class OriginTests(unittest.TestCase):
    @tag("T31")
    def test_T31_system_is_query_only(self):
        self.assertTrue(allowed(Origin.SYSTEM, kind=Kind.QUERY))
        for kind in (Kind.CHAT, Kind.BEHAVIOUR, Kind.ACTION):
            with self.subTest(kind=kind):
                self.assertFalse(allowed(Origin.SYSTEM, kind=kind))

    @tag("T32")
    def test_T32_missing_origin_denied(self):
        self.assertFalse(allowed(None))

    @tag("T16", "T45")
    def test_T16_T45_prefilled_never_becomes_user_at(self):
        self.assertFalse(allowed(Origin.AI_PREFILL))
        self.assertFalse(allowed(Origin.USER_AT, Provenance.PREFILLED))
        self.assertFalse(allowed(Origin.USER_AT, Provenance.PREFILLED, paste_confirmed=True))

    @tag("T17")
    def test_T17_pasted_needs_confirmation(self):
        self.assertFalse(allowed(Origin.USER_AT, Provenance.PASTED))
        self.assertTrue(allowed(Origin.USER_AT, Provenance.PASTED, paste_confirmed=True))
        self.assertTrue(allowed(Origin.USER_AT, Provenance.TYPED))

    def test_ai_approved_needs_matching_unexpired_approval(self):
        now = [100.0]
        approval = Approval("a1", expires_at=200.0)

        def check(**kw):
            return check_send_allowed(Origin.AI_APPROVED, Provenance.TYPED, Kind.ACTION, clock=lambda: now[0], **kw).allowed

        self.assertFalse(check(action_id="a1"))
        self.assertFalse(check(approval=approval, action_id="other"))
        self.assertTrue(check(approval=approval, action_id="a1"))
        now[0] = 201.0
        self.assertFalse(check(approval=approval, action_id="a1"))


class StoreTests(FakeCliTestCase):
    def _context(self, **kw):
        base = dict(
            action_id=new_action_id(), kind=Kind.CHAT, label="게임 채팅 전송", origin="USER_AT",
            command="write_chat", created_at=1.0, owner_pid=os.getpid(), steps=[Step("chat")],
        )
        base.update(kw)
        return ActionContext(**base)

    def test_roundtrip_has_no_chat_text_fields(self):
        store = self.store()
        context = self._context()
        store.save(context)
        loaded = store.get(context.action_id)
        self.assertEqual(loaded.steps[0].state, StepState.PENDING)
        self.assertNotIn("message", context.to_dict())

    def test_save_failure_raises_store_error(self):
        store = self.store()
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(StoreError):
                store.save(self._context())

    def test_corrupt_file_is_moved_aside(self):
        store = self.store()
        store.save(self._context())
        (self.store_dir / "bad.json").write_text("{", encoding="utf-8")
        self.assertEqual(len(store.load_all()), 1)
        self.assertTrue((self.store_dir / "bad.corrupt").exists())

    @tag("C31", "T22")
    def test_C31_dead_owner_pending_becomes_unresolved(self):
        store = self.store(owner_probe=lambda pid, start: OwnerState.DEAD)
        context = self._context(owner_pid=999999)
        store.save(context)
        summary = store.close_orphans()
        self.assertEqual(summary["closed"], 1)
        loaded = store.get(context.action_id)
        self.assertEqual(loaded.outcome, Outcome.UNRESOLVED)
        self.assertEqual(loaded.steps[0].state, StepState.UNKNOWN)

    def test_alive_owner_is_left_alone(self):
        store = self.store(owner_probe=lambda pid, start: OwnerState.ALIVE)
        context = self._context()
        store.save(context)
        store.close_orphans()
        self.assertIsNone(store.get(context.action_id).outcome)

    def test_undetermined_owner_is_not_changed_but_counts_for_confirmation(self):
        store = self.store(owner_probe=lambda pid, start: OwnerState.UNKNOWN)
        context = self._context()
        store.save(context)
        summary = store.close_orphans()
        self.assertEqual(summary, {"closed": 0, "undetermined": 1})
        self.assertIsNone(store.get(context.action_id).outcome)
        self.assertTrue(store.has_undetermined_pending())

    def test_acknowledge_only_chat_kinds(self):
        store = self.store()
        chat = self._context(outcome=Outcome.UNRESOLVED)
        action = self._context(kind=Kind.ACTION, outcome=Outcome.UNRESOLVED)
        store.save(chat)
        store.save(action)
        self.assertEqual(store.acknowledge_unresolved(), 1)
        self.assertTrue(store.get(chat.action_id).acknowledged)
        self.assertFalse(store.get(action.action_id).acknowledged)


if __name__ == "__main__":
    unittest.main()
