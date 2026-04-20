from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import websockets

from tools.tts_common import (
    AudioSink,
    DEFAULT_DEVICE,
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    build_stream_url,
    is_server_healthy,
    launch_server,
    normalize_tts_text,
    wait_for_server,
)


class VibeVoiceSpeaker:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        voice: Optional[str] = None,
        cfg: float = 1.5,
        steps: Optional[int] = None,
        play_audio: bool = True,
        output_path: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.voice = voice
        self.cfg = cfg
        self.steps = steps
        self.sink = AudioSink(
            play_audio=play_audio,
            output_path=output_path,
            sample_rate=24_000,
        )

    async def _speak_async(self, text: str) -> None:
        url = build_stream_url(
            text=text,
            host=self.host,
            port=self.port,
            voice=self.voice,
            cfg=self.cfg,
            steps=self.steps,
        )
        async with websockets.connect(url, max_size=None) as websocket:
            while True:
                try:
                    message = await websocket.recv()
                except websockets.ConnectionClosedOK:
                    break
                except websockets.ConnectionClosed as exc:
                    raise RuntimeError(f"VibeVoice websocket closed unexpectedly: {exc}") from exc

                if isinstance(message, bytes):
                    self.sink.write(message)
                    continue

                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event = payload.get("event")
                if event == "generation_error":
                    message_text = payload.get("data", {}).get("message", "Unknown generation error")
                    raise RuntimeError(message_text)

    def speak(self, text: str) -> None:
        asyncio.run(self._speak_async(text))

    def close(self) -> None:
        self.sink.close()


def read_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --text-file, or pipe text via stdin.")


def ensure_server(args: argparse.Namespace) -> Optional[subprocess.Popen]:
    if is_server_healthy(host=args.host, port=args.port):
        return None
    if not args.auto_start_server:
        raise SystemExit(
            f"VibeVoice server is not reachable at http://{args.host}:{args.port}. "
            "Start it first or pass --auto-start-server."
        )
    process = launch_server(
        host=args.host,
        port=args.port,
        model=args.model,
        device=args.device,
    )
    if not wait_for_server(host=args.host, port=args.port, timeout=args.server_timeout):
        process.terminate()
        raise SystemExit(
            "Timed out while waiting for the VibeVoice server to become ready. "
            "Check logs/vibevoice-server.log for details."
        )
    return process


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone VibeVoice websocket client.")
    parser.add_argument("--text", type=str, help="Text to speak.")
    parser.add_argument("--text-file", type=str, help="Path to a UTF-8 text file to speak.")
    parser.add_argument("--output", type=Path, help="Optional WAV output path.")
    parser.add_argument("--voice", type=str, help="Voice key from /config.")
    parser.add_argument("--cfg", type=float, default=1.5, help="Classifier-free guidance scale.")
    parser.add_argument("--steps", type=int, help="Optional diffusion steps override.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="VibeVoice server host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="VibeVoice server port.")
    parser.add_argument("--no-play", action="store_true", help="Do not play audio, only stream/save.")
    parser.add_argument("--auto-start-server", action="store_true", help="Start the local websocket server if needed.")
    parser.add_argument("--server-timeout", type=float, default=600.0, help="Seconds to wait for server readiness.")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Model path or Hugging Face repo ID.")
    parser.add_argument("--device", type=str, default=DEFAULT_DEVICE, help="Server device to use if auto-starting.")
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
