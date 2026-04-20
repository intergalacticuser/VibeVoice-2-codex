from __future__ import annotations

import unittest

from tools.tts_common import IncrementalSentenceBuffer, split_text_for_tts


class TTSCommonTests(unittest.TestCase):
    def test_split_text_for_tts_breaks_long_clause_heavy_sentence(self) -> None:
        text = (
            "This is a very long sentence, with several comma-separated ideas, "
            "that should not be spoken as one runaway chunk, because the voice gets unstable."
        )
        chunks = split_text_for_tts(text, max_chars=70)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 70 for chunk in chunks))

    def test_incremental_buffer_flushes_shorter_ready_sentence(self) -> None:
        buffer = IncrementalSentenceBuffer(min_chars=24, max_chars=140)
        first = buffer.push("This sentence should flush once it ends.")
        self.assertEqual(first, ["This sentence should flush once it ends."])


if __name__ == "__main__":
    unittest.main()
