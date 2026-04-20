from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.tts_common import (
    DEFAULT_APPLE_VOICE,
    DEFAULT_BACKEND,
    DEFAULT_DEVICE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    normalize_tts_text,
    resolve_backend,
    resolve_model_name,
)


def read_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --text-file, or pipe text via stdin.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Speak text through VibeVoice using either the official or Apple backend.")
    parser.add_argument(
        "--backend",
        type=str,
        choices=("auto", "official", "apple"),
        default=DEFAULT_BACKEND,
        help="Speech backend. `apple` uses MLX on Apple Silicon, `official` uses the Microsoft websocket demo.",
    )
    parser.add_argument("--text", type=str, help="Text to speak.")
    parser.add_argument("--text-file", type=str, help="Path to a UTF-8 text file to speak.")
    parser.add_argument("--output", type=Path, help="Optional WAV output path.")
    parser.add_argument("--voice", type=str, help="Voice key or speaker name.")
    parser.add_argument("--cfg", type=float, default=1.5, help="Classifier-free guidance scale.")
    parser.add_argument("--steps", type=int, help="Optional diffusion steps override.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="VibeVoice server host for the official backend.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="VibeVoice server port for the official backend.")
    parser.add_argument("--no-play", action="store_true", help="Do not play audio, only save/stream.")
    parser.add_argument("--auto-start-server", action="store_true", help="Start the official websocket server if needed.")
    parser.add_argument("--server-timeout", type=float, default=600.0, help="Seconds to wait for official server readiness.")
    parser.add_argument("--model", type=str, help="Model path or Hugging Face repo ID.")
    parser.add_argument("--device", type=str, default=DEFAULT_DEVICE, help="Official backend device when auto-starting.")
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

    backend = resolve_backend(args.backend)
    model_name = resolve_model_name(backend, args.model)

    if backend == "apple":
        from tools.mlx_vibevoice import MLXVibeVoiceSpeaker

        speaker = MLXVibeVoiceSpeaker(
            model=model_name,
            voice=args.voice or DEFAULT_APPLE_VOICE,
            cfg=args.cfg,
            steps=args.steps,
            play_audio=not args.no_play,
            output_path=args.output,
        )
        try:
            speaker.speak(text)
        finally:
            speaker.close()
        return

    from tools.vibevoice_ws_client import VibeVoiceSpeaker, ensure_server

    args.model = model_name
    server_process = ensure_server(args)
    speaker = VibeVoiceSpeaker(
        host=args.host,
        port=args.port,
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
        if server_process is not None:
            server_process.terminate()
            server_process.wait(timeout=10)


if __name__ == "__main__":
    main()
