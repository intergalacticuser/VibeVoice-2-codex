from __future__ import annotations

import unittest

from tools.voice_bridge_common import BridgeSettings, chunk_config_for_settings


class VoiceBridgeCommonTests(unittest.TestCase):
    def test_status_only_mode_syncs_legacy_fields(self) -> None:
        settings = BridgeSettings.from_dict(
            {
                "desktop_speech_mode": "status_only",
                "commentary_voice_mode": "original_text",
                "desktop_text_mode": "original_text",
            }
        )

        self.assertEqual(settings.desktop_speech_mode, "status_only")
        self.assertEqual(settings.commentary_voice_mode, "english_status_only")
        self.assertEqual(settings.desktop_text_mode, "english_reports_only")

    def test_live_fast_mode_syncs_legacy_fields(self) -> None:
        settings = BridgeSettings.from_dict(
            {
                "desktop_speech_mode": "live_fast",
                "commentary_voice_mode": "english_status_only",
                "desktop_text_mode": "english_reports_only",
            }
        )

        self.assertEqual(settings.desktop_speech_mode, "live_fast")
        self.assertEqual(settings.commentary_voice_mode, "original_text")
        self.assertEqual(settings.desktop_text_mode, "original_text")

    def test_english_full_mode_syncs_translation_field(self) -> None:
        settings = BridgeSettings.from_dict(
            {
                "desktop_speech_mode": "english_full",
                "commentary_voice_mode": "english_status_only",
                "desktop_text_mode": "english_reports_only",
            }
        )

        self.assertEqual(settings.desktop_speech_mode, "english_full")
        self.assertEqual(settings.commentary_voice_mode, "original_text")
        self.assertEqual(settings.desktop_text_mode, "translate_to_english")

    def test_legacy_everything_alias_maps_to_live_fast(self) -> None:
        settings = BridgeSettings.from_dict({"desktop_speech_mode": "everything"})
        self.assertEqual(settings.desktop_speech_mode, "live_fast")

    def test_legacy_english_only_alias_maps_to_english_full(self) -> None:
        settings = BridgeSettings.from_dict({"desktop_speech_mode": "english_only"})
        self.assertEqual(settings.desktop_speech_mode, "english_full")

    def test_live_fast_uses_smaller_chunk_budget(self) -> None:
        settings = BridgeSettings.from_dict({"desktop_speech_mode": "live_fast"})
        chunk_config = chunk_config_for_settings(settings)
        self.assertEqual(chunk_config.emit_max_chars, 110)
        self.assertEqual(chunk_config.merge_max_chars, 140)

    def test_english_full_uses_balanced_chunk_budget(self) -> None:
        settings = BridgeSettings.from_dict({"desktop_speech_mode": "english_full"})
        chunk_config = chunk_config_for_settings(settings)
        self.assertEqual(chunk_config.emit_max_chars, 140)
        self.assertEqual(chunk_config.merge_max_chars, 220)


if __name__ == "__main__":
    unittest.main()
