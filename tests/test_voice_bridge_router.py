from __future__ import annotations

import unittest

from tools.voice_bridge_common import BridgeSettings
from tools.voice_bridge_router import CodexSpeechRouter


class VoiceBridgeRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = BridgeSettings()
        self.emitted = []
        self.interrupts = []
        self.router = CodexSpeechRouter(
            settings_provider=lambda: self.settings,
            emit=self._emit,
            interrupt=self.interrupts.append,
        )

    def _emit(
        self,
        channel: str,
        text: str,
        source_item_id: str,
        turn_id: str,
        group_key: str,
        interruptible: bool,
    ) -> None:
        self.emitted.append(
            {
                "channel": channel,
                "text": text,
                "source_item_id": source_item_id,
                "turn_id": turn_id,
                "group_key": group_key,
                "interruptible": interruptible,
            }
        )

    def test_assistant_deltas_skip_completed_duplicate(self) -> None:
        self.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "First sentence. Second sentence."},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/completed",
                "params": {"turnId": "turn-1", "item": {"id": "msg-1", "type": "agentMessage", "text": "First sentence. Second sentence."}},
            }
        )
        channels = [item["channel"] for item in self.emitted]
        self.assertIn("assistant_deltas", channels)
        self.assertNotIn("assistant_completed", channels)

    def test_completed_fallback_when_deltas_disabled(self) -> None:
        self.settings.speak_assistant_deltas = False
        self.settings.speak_status_announcements = False
        self.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "This should only be spoken at completion."},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-1",
                    "item": {"id": "msg-1", "type": "agent_message", "text": "This should only be spoken at completion."},
                },
            }
        )
        self.assertEqual([item["channel"] for item in self.emitted], ["assistant_completed"])

    def test_reasoning_summary_ignores_raw_reasoning_deltas(self) -> None:
        self.router.handle_payload(
            {
                "method": "item/reasoning/textDelta",
                "params": {"turnId": "turn-1", "itemId": "reason-1", "delta": "private chain of thought"},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {"turnId": "turn-1", "itemId": "reason-1", "summaryIndex": 0, "delta": "Safe summary. More safe summary."},
            }
        )
        self.assertEqual([item["channel"] for item in self.emitted], ["status_announcements", "reasoning_summary"])
        self.assertNotIn("private chain of thought", " ".join(item["text"] for item in self.emitted))

    def test_default_finish_current_keeps_reading_existing_plan(self) -> None:
        self.router.handle_payload(
            {
                "method": "item/plan/delta",
                "params": {"turnId": "turn-1", "itemId": "plan-1", "delta": "Unfinished plan item that should still be spoken to the end"},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "New visible answer. It should wait its turn."},
            }
        )
        self.router.flush_all()
        self.assertEqual(self.interrupts, [])
        spoken = " ".join(item["text"] for item in self.emitted)
        self.assertIn("Unfinished plan item", spoken)
        self.assertIn("New visible answer.", spoken)

    def test_interrupt_latest_can_drop_stale_plan_buffer(self) -> None:
        self.settings.interrupt_policy = "interrupt_latest"
        self.router.handle_payload(
            {
                "method": "item/plan/delta",
                "params": {"turnId": "turn-1", "itemId": "plan-1", "delta": "Unfinished plan item that should never flush after interruption"},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "New visible answer. It should take over immediately."},
            }
        )
        self.router.flush_all()
        self.assertEqual(self.interrupts[-1], "turn-1:agentMessage:msg-1")
        spoken = " ".join(item["text"] for item in self.emitted)
        self.assertIn("New visible answer.", spoken)
        self.assertNotIn("Unfinished plan item", spoken)

    def test_read_through_disables_auto_interrupt(self) -> None:
        self.settings.interrupt_policy = "interrupt_latest"
        self.settings.read_through = True
        self.router.handle_payload(
            {
                "method": "item/plan/delta",
                "params": {"turnId": "turn-1", "itemId": "plan-1", "delta": "Plan sentence."},
            }
        )
        self.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "Answer sentence."},
            }
        )
        self.assertEqual(self.interrupts, [])


if __name__ == "__main__":
    unittest.main()
