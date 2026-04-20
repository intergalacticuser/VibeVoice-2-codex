from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from tools.codex_desktop_voice import ActiveDesktopSessionManager, DesktopSessionBridgeAdapter, ThreadRecord, select_active_thread
from tools.voice_bridge_common import BridgeSettings
from tools.voice_bridge_router import CodexSpeechRouter


class FakeEnglishTranslator:
    def translate(self, text: str) -> str:
        mapping = {
            "Русский комментарий для прогресса.": "English commentary update for progress tracking.",
            "Финальный ответ на русском.": "Final answer in English.",
            "Проверяю голосовой мост.": "Checking the voice bridge.",
            "Первое предложение.": "First sentence.",
            "Второе предложение.": "Second sentence.",
        }
        return mapping.get(text, f"EN[{text}]")


class DesktopSessionBridgeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emitted = []
        settings = BridgeSettings()
        settings.desktop_text_mode = "original_text"
        self.router = CodexSpeechRouter(
            settings_provider=lambda: settings,
            emit=lambda channel, text, source_item_id, turn_id, group_key, interruptible: self.emitted.append(
                {
                    "channel": channel,
                    "text": text,
                    "source_item_id": source_item_id,
                    "turn_id": turn_id,
                    "group_key": group_key,
                    "interruptible": interruptible,
                }
            ),
            interrupt=lambda group_key: None,
        )
        self.adapter = DesktopSessionBridgeAdapter(
            controller=SimpleNamespace(router=self.router, settings=settings),
            thread_id="thread-1",
            cwd="/tmp/workspace",
            english_translator=FakeEnglishTranslator(),
        )

    def test_event_msg_agent_message_becomes_spoken_assistant_text(self) -> None:
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Live desktop answer.", "phase": "commentary"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:02Z",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "Live desktop answer."},
            },
            emit=True,
        )

        self.assertEqual(
            [item["channel"] for item in self.emitted],
            ["status_announcements", "status_announcements", "assistant_deltas", "status_announcements"],
        )
        self.assertEqual(self.emitted[2]["text"], "Live desktop answer.")

    def test_response_item_message_is_deduped_against_agent_message(self) -> None:
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            emit=True,
        )
        record = {
            "timestamp": "2026-04-18T12:00:01Z",
            "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Same answer."}]},
        }
        self.adapter.process_record({"type": "event_msg", **record, "payload": {"type": "agent_message", "message": "Same answer.", "phase": "final_answer"}}, emit=True)
        self.adapter.process_record({"type": "response_item", **record}, emit=True)
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:02Z",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "Same answer."},
            },
            emit=True,
        )

        visible = [item for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["text"], "Same answer.")

    def test_reasoning_summary_is_spoken_and_raw_reasoning_is_not(self) -> None:
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "response_item",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Checking the live voice path."}],
                    "encrypted_content": "hidden",
                },
            },
            emit=True,
        )

        self.assertEqual([item["channel"] for item in self.emitted], ["status_announcements", "status_announcements", "reasoning_summary"])
        self.assertEqual(self.emitted[-1]["text"], "Checking the live voice path.")

    def test_commentary_can_be_suppressed_into_english_status_only_mode(self) -> None:
        self.adapter.settings.commentary_voice_mode = "english_status_only"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Русский комментарий для прогресса.", "phase": "commentary"},
            },
            emit=True,
        )

        self.assertEqual([item["channel"] for item in self.emitted], ["status_announcements"])
        self.assertEqual(self.emitted[0]["text"], "Working.")

    def test_english_reports_only_proxies_non_english_commentary(self) -> None:
        self.adapter.settings.desktop_text_mode = "english_reports_only"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Русский комментарий для прогресса.", "phase": "commentary"},
            },
            emit=True,
        )

        visible = [item for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["text"], "Progress update from the active chat.")

    def test_english_reports_only_proxies_non_english_final_answer(self) -> None:
        self.adapter.settings.desktop_text_mode = "english_reports_only"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-9"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Финальный ответ на русском.", "phase": "final_answer"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:02Z",
                "payload": {"type": "task_complete", "turn_id": "turn-9", "last_agent_message": "Финальный ответ на русском."},
            },
            emit=True,
        )

        visible = [item for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["text"], "Answer ready.")

    def test_translate_to_english_converts_non_english_commentary(self) -> None:
        self.adapter.settings.desktop_text_mode = "translate_to_english"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-5"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Русский комментарий для прогресса.", "phase": "commentary"},
            },
            emit=True,
        )

        visible = [item for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["text"], "English commentary update for progress tracking.")

    def test_translate_to_english_converts_reasoning_summary(self) -> None:
        self.adapter.settings.desktop_text_mode = "translate_to_english"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-6"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "response_item",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Проверяю голосовой мост."}],
                },
            },
            emit=True,
        )

        self.assertEqual(self.emitted[-1]["channel"], "reasoning_summary")
        self.assertEqual(self.emitted[-1]["text"], "Checking the voice bridge.")

    def test_translate_to_english_uses_delta_suffix_when_commentary_grows(self) -> None:
        self.adapter.settings.desktop_text_mode = "translate_to_english"
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-7"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Первое предложение.", "phase": "commentary"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:02Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Первое предложение. Второе предложение.",
                    "phase": "commentary",
                },
            },
            emit=True,
        )

        visible = [item["text"] for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}]
        self.assertEqual(visible[-1], "First sentence. Second sentence.")
        self.assertEqual(visible[-1].count("First sentence."), 1)

    def test_cumulative_commentary_speaks_only_the_new_suffix(self) -> None:
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-4"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "agent_message", "message": "First sentence.", "phase": "commentary"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:02Z",
                "payload": {"type": "agent_message", "message": "First sentence. Second sentence.", "phase": "commentary"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:03Z",
                "payload": {"type": "task_complete", "turn_id": "turn-4", "last_agent_message": "First sentence. Second sentence."},
            },
            emit=True,
        )

        visible_text = " ".join(
            item["text"] for item in self.emitted if item["channel"] in {"assistant_completed", "assistant_deltas"}
        )
        self.assertIn("First sentence.", visible_text)
        self.assertIn("Second sentence.", visible_text)
        self.assertEqual(visible_text.count("First sentence."), 1)

    def test_tool_call_emits_status_once(self) -> None:
        self.adapter.process_record(
            {
                "type": "event_msg",
                "timestamp": "2026-04-18T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "turn-3"},
            },
            emit=True,
        )
        self.adapter.process_record(
            {
                "type": "response_item",
                "timestamp": "2026-04-18T12:00:01Z",
                "payload": {"type": "function_call", "call_id": "tool-1", "name": "exec_command"},
            },
            emit=True,
        )

        statuses = [item["text"] for item in self.emitted if item["channel"] == "status_announcements"]
        self.assertEqual(statuses, ["Working.", "Running tool."])


class ActiveThreadSelectionTests(unittest.TestCase):
    def test_select_active_thread_prefers_active_workspace(self) -> None:
        records = [
            ThreadRecord("thread-b", Path("/tmp/b.jsonl"), "/workspace-b", "B", 50),
            ThreadRecord("thread-a-latest", Path("/tmp/a-2.jsonl"), "/workspace-a", "A2", 40),
            ThreadRecord("thread-a-older", Path("/tmp/a-1.jsonl"), "/workspace-a", "A1", 10),
        ]

        selected = select_active_thread(records, ["/workspace-a"])

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.thread_id, "thread-a-latest")

    def test_select_active_thread_uses_fallback_thread_when_workspace_missing(self) -> None:
        records = [
            ThreadRecord("thread-b", Path("/tmp/b.jsonl"), "/workspace-b", "B", 50),
            ThreadRecord("thread-a", Path("/tmp/a.jsonl"), "/workspace-a", "A", 40),
        ]

        selected = select_active_thread(records, ["/workspace-missing"], fallback_thread_id="thread-a")

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.thread_id, "thread-a")

    def test_switching_threads_resets_existing_speech_state(self) -> None:
        events = []

        class FakeTail:
            def __init__(self, *, thread_record, controller, poll_interval, history_bytes, mirror_writer) -> None:
                self.thread_record = thread_record
                events.append(("init", thread_record.thread_id))

            def open(self, stop_event) -> None:
                events.append(("open", self.thread_record.thread_id))

            def close(self) -> None:
                events.append(("close", self.thread_record.thread_id))

        controller = SimpleNamespace(
            reset_speech_state=lambda **kwargs: events.append(("reset", kwargs)),
        )
        manager = ActiveDesktopSessionManager(
            controller=controller,
            poll_interval=0.1,
            history_bytes=1024,
        )
        first = ThreadRecord("thread-a", Path("/tmp/a.jsonl"), "/workspace-a", "A", 1)
        second = ThreadRecord("thread-b", Path("/tmp/b.jsonl"), "/workspace-b", "B", 2)

        with patch("tools.codex_desktop_voice.DesktopSessionTail", FakeTail):
            manager._switch_to(first)
            manager._switch_to(second)

        self.assertIn(("open", "thread-a"), events)
        self.assertIn(("close", "thread-a"), events)
        self.assertIn(("open", "thread-b"), events)
        self.assertEqual(
            [event for event in events if event[0] == "reset"],
            [("reset", {"cancel_current": True, "clear_queue": True, "reset_router": True})],
        )


if __name__ == "__main__":
    unittest.main()
