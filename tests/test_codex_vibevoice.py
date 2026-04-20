from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.voice_bridge_backends import BaseSpeechBackend
from tools.voice_bridge_common import BridgeSettings


class FakeBackend(BaseSpeechBackend):
    def __init__(self) -> None:
        self.stop_calls = 0
        self.voice = None
        self.speed = 1.0

    def speak(self, text: str) -> bool:
        return False

    def stop(self) -> None:
        self.stop_calls += 1

    def set_voice(self, voice):
        self.voice = voice

    def set_speed(self, speed: float) -> None:
        self.speed = speed

    def close(self) -> None:
        return None


class VoiceBridgeControllerTests(unittest.TestCase):
    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            prompt=None,
            backend="apple",
            voice=None,
            speed=None,
            cfg=1.5,
            steps=None,
            host="127.0.0.1",
            port=3000,
            model=None,
            device="mps",
            panel=False,
            control_port=30123,
            include_reasoning_summary=False,
            speak_code=False,
            interrupt_policy="finish_current",
            skip_git_repo_check=False,
            auto_start_server=False,
            codex_args=[],
            speak_assistant_deltas=None,
            speak_assistant_completed=None,
            speak_reasoning_summary=None,
            speak_status_announcements=None,
        )

    def test_new_turn_prunes_stale_queued_items(self) -> None:
        backend = FakeBackend()
        with patch("tools.codex_vibevoice.create_backend", return_value=backend), patch(
            "tools.codex_vibevoice.load_settings", return_value=BridgeSettings()
        ), patch("tools.codex_vibevoice.save_settings", return_value=None), patch(
            "tools.codex_vibevoice.resolve_model_name", return_value="fake-model"
        ):
            from tools.codex_vibevoice import VoiceBridgeController

            controller = VoiceBridgeController(self._args())

        controller._emit_speech("assistant_deltas", "old one", "msg-1", "turn-1", "turn-1:agentMessage:msg-1", True)
        controller._emit_speech("assistant_deltas", "old two", "msg-2", "turn-1", "turn-1:agentMessage:msg-2", True)
        controller._emit_speech("assistant_deltas", "new one", "msg-3", "turn-2", "turn-2:agentMessage:msg-3", True)

        with controller._condition:
            queued_turns = [item.turn_id for item in controller._queue]
            queued_texts = [item.text for item in controller._queue]

        self.assertEqual(queued_turns, ["turn-2"])
        self.assertEqual(queued_texts, ["new one"])

    def test_reset_speech_state_clears_queue_and_stops_backend(self) -> None:
        backend = FakeBackend()
        with patch("tools.codex_vibevoice.create_backend", return_value=backend), patch(
            "tools.codex_vibevoice.load_settings", return_value=BridgeSettings()
        ), patch("tools.codex_vibevoice.save_settings", return_value=None), patch(
            "tools.codex_vibevoice.resolve_model_name", return_value="fake-model"
        ):
            from tools.codex_vibevoice import VoiceBridgeController

            controller = VoiceBridgeController(self._args())

        controller._emit_speech("assistant_deltas", "queued", "msg-1", "turn-1", "turn-1:agentMessage:msg-1", True)
        controller.router.handle_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "buffered sentence."},
            }
        )

        controller.reset_speech_state(cancel_current=True, clear_queue=True, reset_router=True)

        with controller._condition:
            self.assertEqual(controller._queue, [])
        self.assertEqual(backend.stop_calls, 1)
        self.assertEqual(controller.router._assistant_buffers, {})

    def test_stop_all_uses_full_reset(self) -> None:
        backend = FakeBackend()
        with patch("tools.codex_vibevoice.create_backend", return_value=backend), patch(
            "tools.codex_vibevoice.load_settings", return_value=BridgeSettings()
        ), patch("tools.codex_vibevoice.save_settings", return_value=None), patch(
            "tools.codex_vibevoice.resolve_model_name", return_value="fake-model"
        ):
            from tools.codex_vibevoice import VoiceBridgeController

            controller = VoiceBridgeController(self._args())

        controller._emit_speech("assistant_deltas", "queued", "msg-1", "turn-1", "turn-1:agentMessage:msg-1", True)
        controller.stop_all()

        with controller._condition:
            self.assertEqual(controller._queue, [])
        self.assertEqual(backend.stop_calls, 1)
        self.assertEqual(controller.router._assistant_buffers, {})


if __name__ == "__main__":
    unittest.main()
