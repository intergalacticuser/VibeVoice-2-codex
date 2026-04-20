from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import numpy as np
import sounddevice as sd
from mlx_audio.audio_io import write as audio_write
from mlx_audio.tts.utils import load_model

from tools.tts_common import DEFAULT_APPLE_MODEL, DEFAULT_APPLE_VOICE, normalize_tts_text


class MLXVibeVoiceSpeaker:
    def __init__(
        self,
        *,
        model: str = DEFAULT_APPLE_MODEL,
        voice: Optional[str] = None,
        cfg: float = 1.5,
        steps: Optional[int] = None,
        play_audio: bool = True,
        output_path: Optional[Path] = None,
        max_tokens: int = 1_200,
    ) -> None:
        self.model_name = model
        self.voice = voice or DEFAULT_APPLE_VOICE
        self.cfg = cfg
        self.steps = steps
        self.play_audio = play_audio
        self.output_path = output_path
        self.max_tokens = max_tokens
        self._model = None
        self._audio_chunks: List[np.ndarray] = []
        self._sample_rate: Optional[int] = None

    def _ensure_loaded(self):
        if self._model is None:
            self._model = load_model(self.model_name)
        return self._model

    def speak(self, text: str) -> None:
        model = self._ensure_loaded()
        kwargs = {
            "text": text,
            "voice": self.voice,
            "cfg_scale": self.cfg,
            "max_tokens": self.max_tokens,
            "verbose": False,
        }
        if self.steps is not None:
            kwargs["ddpm_steps"] = self.steps

        for result in model.generate(**kwargs):
            audio = np.asarray(result.audio, dtype=np.float32)
            if audio.size == 0:
                continue
            self._sample_rate = result.sample_rate
            self._audio_chunks.append(audio.copy())

        if not self._audio_chunks or self._sample_rate is None:
            raise RuntimeError("The Apple MLX speaker produced no audio chunks.")

        if self.play_audio:
            joined = np.concatenate(self._audio_chunks, axis=0)
            if platform.system() == "Darwin":
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                    temp_path = Path(temp_file.name)
                try:
                    audio_write(str(temp_path), joined, self._sample_rate, format="wav")
                    subprocess.run(["/usr/bin/afplay", str(temp_path)], check=True)
                finally:
                    temp_path.unlink(missing_ok=True)
            else:
                sd.play(joined, self._sample_rate)
                sd.wait()

    def close(self) -> None:
        if self.output_path is None or not self._audio_chunks or self._sample_rate is None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        joined = np.concatenate(self._audio_chunks, axis=0)
        audio_format = self.output_path.suffix.lstrip(".") or "wav"
        audio_write(str(self.output_path), joined, self._sample_rate, format=audio_format)


def read_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --text-file, or pipe text via stdin.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone MLX VibeVoice speaker for Apple Silicon.")
    parser.add_argument("--text", type=str, help="Text to speak.")
    parser.add_argument("--text-file", type=str, help="Path to a UTF-8 text file to speak.")
    parser.add_argument("--output", type=Path, help="Optional output path, usually .wav.")
    parser.add_argument("--voice", type=str, default=DEFAULT_APPLE_VOICE, help="MLX VibeVoice voice name.")
    parser.add_argument("--cfg", type=float, default=1.5, help="Classifier-free guidance scale.")
    parser.add_argument("--steps", type=int, help="Optional diffusion steps override.")
    parser.add_argument("--model", type=str, default=DEFAULT_APPLE_MODEL, help="MLX model repo ID or local path.")
    parser.add_argument("--no-play", action="store_true", help="Do not play audio, only save.")
    parser.add_argument(
        "--speak-code",
        action="store_true",
        help="Keep simplified inline code instead of stripping code-like content.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_text = read_text(args)
    text = normalize_tts_text(raw_text, speak_code=args.speak_code)
    if not text:
        raise SystemExit("Nothing left to speak after text normalization.")

    speaker = MLXVibeVoiceSpeaker(
        model=args.model,
        voice=args.voice,
        cfg=args.cfg,
        steps=args.steps,
        play_audio=not args.no_play,
        output_path=args.output,
    )
    try:
        speaker.speak(text)
    finally:
        speaker.close()


if __name__ == "__main__":
    main()
