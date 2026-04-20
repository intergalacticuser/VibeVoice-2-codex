from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket

from tools.tts_common import DEFAULT_BACKEND, DEFAULT_DEVICE, DEFAULT_HOST, DEFAULT_PORT, resolve_backend, resolve_model_name
from tools.voice_bridge_backends import BaseSpeechBackend, create_backend
from tools.voice_bridge_common import (
    CHANNELS,
    INTERRUPT_POLICIES,
    DESKTOP_SPEECH_MODES,
    BridgeSettings,
    BridgeSnapshot,
    SpeechItem,
    apply_desktop_speech_mode,
    allocate_control_port,
    clear_session,
    chunk_config_for_settings,
    load_settings,
    normalize_desktop_speech_mode,
    save_settings,
    serialize_item,
    truncate_preview,
    write_session,
)
from tools.voice_bridge_router import CodexSpeechRouter


def normalize_event_name(value: Optional[str]) -> str:
    return (value or "").lower().replace("/", "").replace(".", "").replace("_", "")


class VoiceBridgeController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings = self._build_settings(args)
        self.model_name = resolve_model_name(self.settings.backend, args.model or self.settings.model)
        self.settings.model = self.model_name
        save_settings(self.settings)

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queue: List[SpeechItem] = []
        self._current: Optional[SpeechItem] = None
        self._shutdown = False
        self._speech_thread = threading.Thread(target=self._speech_loop, daemon=True)
        self._backend: BaseSpeechBackend = create_backend(self.settings, model_name=self.model_name)
        self._backend_ready = True
        self._backend_error: Optional[str] = None
        self._codex_process: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._uvicorn_thread: Optional[threading.Thread] = None
        self._panel_process: Optional[subprocess.Popen] = None
        self.control_port = args.control_port or allocate_control_port()
        self.control_url = f"http://127.0.0.1:{self.control_port}"
        self.router = CodexSpeechRouter(
            settings_provider=lambda: self.settings,
            emit=self._emit_speech,
            interrupt=self._interrupt_for_group,
        )

    def _build_settings(self, args: argparse.Namespace) -> BridgeSettings:
        settings = load_settings()
        settings.backend = resolve_backend(args.backend or settings.backend or DEFAULT_BACKEND)
        settings.host = args.host or settings.host or DEFAULT_HOST
        settings.port = args.port or settings.port or DEFAULT_PORT
        settings.model = args.model or settings.model
        settings.speak_code = bool(args.speak_code) if args.speak_code else settings.speak_code
        settings.auto_start_server = settings.auto_start_server if args.auto_start_server is None else args.auto_start_server

        if args.voice is not None:
            settings.voice = args.voice
        if args.speed is not None:
            settings.speed = float(args.speed)
        if args.cfg is not None:
            settings.cfg = float(args.cfg)
        if args.steps is not None:
            settings.steps = int(args.steps)
        if args.interrupt_policy is not None:
            settings.interrupt_policy = args.interrupt_policy

        for field_name in (
            "speak_assistant_deltas",
            "speak_assistant_completed",
            "speak_reasoning_summary",
            "speak_status_announcements",
        ):
            value = getattr(args, field_name)
            if value is not None:
                setattr(settings, field_name, value)

        if args.include_reasoning_summary:
            settings.speak_reasoning_summary = True

        save_settings(settings)
        return settings

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = BridgeSnapshot(
                settings=self.settings.to_dict(),
                current=serialize_item(self._current),
                queue=[serialize_item(item) for item in self._queue],
                backend_ready=self._backend_ready,
                backend_error=self._backend_error,
                codex_running=bool(self._codex_process and self._codex_process.poll() is None),
                control_url=self.control_url,
                pid=os.getpid(),
            )
        return snapshot.__dict__

    def _emit_speech(
        self,
        channel: str,
        text: str,
        source_item_id: str,
        turn_id: str,
        group_key: str,
        interruptible: bool,
    ) -> None:
        item = SpeechItem(
            id=f"{group_key}:{len(text)}:{time.time_ns()}",
            channel=channel,
            text=text,
            source_item_id=source_item_id,
            turn_id=turn_id,
            group_key=group_key,
            interruptible=interruptible,
        )
        with self._condition:
            self._queue = [queued for queued in self._queue if queued.turn_id == turn_id]
            self._queue.append(item)
            self._condition.notify_all()

    def _interrupt_for_group(self, group_key: str) -> None:
        with self._condition:
            current_group = self._current.group_key if self._current else None
            if current_group == group_key:
                return
            self._queue.clear()
        self._backend.stop()

    def _speech_loop(self) -> None:
        while True:
            with self._condition:
                while not self._shutdown and not self._queue:
                    self._condition.wait(timeout=0.25)
                if self._shutdown:
                    return
                item = self._queue.pop(0)
                merged_item = self._collect_mergeable_batch(item)
                self._current = merged_item
            try:
                interrupted = self._backend.speak(merged_item.text)
            except BaseException as exc:  # pragma: no cover - runtime surface
                self._backend_error = str(exc)
                interrupted = True
            finally:
                with self._condition:
                    if self._current and self._current.id == merged_item.id:
                        self._current = None
                    self._condition.notify_all()

    def _collect_mergeable_batch(self, item: SpeechItem) -> SpeechItem:
        chunk_config = chunk_config_for_settings(self.settings)
        batch = [item]
        total_chars = len(item.text)
        deadline = time.time() + 0.05
        while True:
            remaining = deadline - time.time()
            if remaining > 0:
                self._condition.wait(timeout=remaining)
            if not self._queue:
                break
            next_item = self._queue[0]
            if not self._can_merge_items(batch[-1], next_item, total_chars):
                break
            batch.append(self._queue.pop(0))
            total_chars += 1 + len(next_item.text)
            if total_chars >= chunk_config.merge_max_chars:
                break
        if len(batch) == 1:
            return item
        combined_text = " ".join(part.text.strip() for part in batch if part.text.strip()).strip()
        return replace(item, id=f"{item.id}:merged:{len(batch)}", text=combined_text)

    def _can_merge_items(self, current: SpeechItem, next_item: SpeechItem, total_chars: int) -> bool:
        chunk_config = chunk_config_for_settings(self.settings)
        if current.channel != next_item.channel:
            return False
        if current.group_key != next_item.group_key:
            return False
        if current.turn_id != next_item.turn_id:
            return False
        if current.source_item_id != next_item.source_item_id:
            return False
        if total_chars + 1 + len(next_item.text) > chunk_config.merge_max_chars:
            return False
        return True

    def start(self) -> None:
        self._speech_thread.start()
        self._start_control_api()
        if self.args.panel:
            self._launch_panel()

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            self._queue.clear()
            self._condition.notify_all()
        try:
            self._backend.stop()
        except Exception:
            pass
        self._speech_thread.join(timeout=5)
        self._backend.close()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._uvicorn_thread is not None:
            self._uvicorn_thread.join(timeout=5)
        if self._panel_process is not None and self._panel_process.poll() is None:
            self._panel_process.terminate()
            self._panel_process.wait(timeout=5)
        clear_session()

    def _launch_panel(self) -> None:
        command = [
            str(Path(__file__).resolve().parents[1] / "scripts" / "codex_voice_panel.sh"),
            "--control-url",
            self.control_url,
        ]
        self._panel_process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1])

    def _start_control_api(self) -> None:
        app = FastAPI()
        controller = self

        @app.get("/state")
        def get_state() -> Dict[str, Any]:
            return controller.snapshot()

        @app.post("/settings")
        def update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
            controller.apply_settings(payload)
            return controller.snapshot()

        @app.post("/actions/stop")
        def stop_current() -> Dict[str, Any]:
            controller.stop_current()
            return controller.snapshot()

        @app.post("/actions/next")
        def next_item() -> Dict[str, Any]:
            controller.next_item()
            return controller.snapshot()

        @app.post("/actions/clear")
        def clear_queue_endpoint() -> Dict[str, Any]:
            controller.clear_queue()
            return controller.snapshot()

        @app.post("/actions/stop_all")
        def stop_all_endpoint() -> Dict[str, Any]:
            controller.stop_all()
            return controller.snapshot()

        @app.post("/actions/shutdown")
        def shutdown_bridge() -> Dict[str, Any]:
            controller.request_shutdown()
            return {"ok": True}

        @app.websocket("/ws")
        async def state_stream(websocket: WebSocket) -> None:
            await websocket.accept()
            try:
                while True:
                    await websocket.send_json(controller.snapshot())
                    await asyncio.sleep(0.25)
            except Exception:
                return

        config = uvicorn.Config(app, host="127.0.0.1", port=self.control_port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._uvicorn_thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._uvicorn_server.started:
                break
            time.sleep(0.05)
        write_session(self.control_url, os.getpid())

    def apply_settings(self, payload: Dict[str, Any]) -> None:
        voice_before = self.settings.effective_voice()
        speed_before = self.settings.speed
        for key, value in payload.items():
            if key == "desktop_speech_mode":
                normalized_mode = normalize_desktop_speech_mode(str(value))
                if normalized_mode is None:
                    continue
                apply_desktop_speech_mode(self.settings, normalized_mode)
                continue
            if not hasattr(self.settings, key):
                continue
            if key == "interrupt_policy" and value not in INTERRUPT_POLICIES:
                continue
            if key == "panel_geometry":
                self.settings.panel_geometry = str(value)
                continue
            if key in {"speed", "cfg"}:
                value = float(value)
            elif key == "steps" and value is not None:
                value = int(value)
            setattr(self.settings, key, value)
        save_settings(self.settings)
        voice_after = self.settings.effective_voice()
        if voice_after != voice_before:
            self._backend.set_voice(voice_after)
        if abs(self.settings.speed - speed_before) > 1e-3:
            self._backend.set_speed(self.settings.speed)
        if self.settings.muted:
            with self._condition:
                self._queue.clear()
                self._condition.notify_all()
            self._backend.stop()

    def stop_current(self) -> None:
        self._backend.stop()

    def next_item(self) -> None:
        self._backend.stop()

    def clear_queue(self) -> None:
        with self._condition:
            self._queue.clear()
            self._condition.notify_all()

    def stop_all(self) -> None:
        self.reset_speech_state(cancel_current=True, clear_queue=True, reset_router=True)

    def reset_speech_state(self, *, cancel_current: bool = True, clear_queue: bool = True, reset_router: bool = True) -> None:
        if reset_router:
            self.router.reset()
        with self._condition:
            if clear_queue:
                self._queue.clear()
            self._condition.notify_all()
        if cancel_current:
            self._backend.stop()

    def request_shutdown(self) -> None:
        def _signal_self() -> None:
            time.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_signal_self, daemon=True).start()

    def wait_for_speech_drain(self, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        with self._condition:
            while True:
                if self._current is None and not self._queue:
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(0.25, remaining))

    def run_codex(self) -> int:
        command = build_codex_command(self.args)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        stdin_target = subprocess.DEVNULL if self.args.prompt else (None if sys.stdin.isatty() else subprocess.PIPE)
        process = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            env=env,
            stdin=stdin_target,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._codex_process = process
        self._stderr_thread = stream_stderr(process.stderr)
        try:
            if process.stdin is not None and not self.args.prompt and not sys.stdin.isatty():
                piped_text = sys.stdin.read()
                process.stdin.write(piped_text)
                process.stdin.close()

            assert process.stdout is not None
            for raw_line in process.stdout:
                sys.stdout.write(raw_line)
                sys.stdout.flush()
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.router.handle_payload(payload)
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
        finally:
            self.router.flush_all()
            process.wait()
            self.wait_for_speech_drain()
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
            self._codex_process = None
        return int(process.returncode or 0)


def build_codex_command(args: argparse.Namespace) -> List[str]:
    if args.codex_args:
        command = list(args.codex_args)
        if command and command[0] == "--":
            command = command[1:]
        return command

    command = ["codex", "exec", "--json", "--color", "never"]
    if args.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if args.prompt:
        command.append(args.prompt)
    elif not sys.stdin.isatty():
        command.append("-")
    return command


def stream_stderr(stream) -> threading.Thread:
    def _pump() -> None:
        for line in stream:
            sys.stderr.write(line)
            sys.stderr.flush()

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    return thread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex voice bridge with runtime control API and optional floating panel.")
    parser.add_argument("--prompt", type=str, help="Prompt to send to `codex exec`.")
    parser.add_argument("--backend", type=str, choices=("auto", "official", "apple"), default=DEFAULT_BACKEND)
    parser.add_argument("--voice", type=str, help="Voice key or speaker name.")
    parser.add_argument("--speed", type=float, help="Speech speed multiplier. Apple backend only.")
    parser.add_argument("--cfg", type=float, default=1.5, help="Classifier-free guidance scale.")
    parser.add_argument("--steps", type=int, help="Optional diffusion steps override.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="Official VibeVoice server host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Official VibeVoice server port.")
    parser.add_argument("--model", type=str, help="Model path or Hugging Face repo ID.")
    parser.add_argument("--device", type=str, default=DEFAULT_DEVICE, help="Reserved for compatibility with the old CLI.")
    parser.add_argument("--panel", action="store_true", help="Launch the floating desktop control panel.")
    parser.add_argument("--control-port", type=int, help="Optional fixed loopback port for the control API.")
    parser.add_argument("--include-reasoning-summary", action="store_true", help="Compatibility alias for enabling reasoning summaries.")
    parser.add_argument("--speak-code", action="store_true", help="Keep simplified inline code instead of stripping code-like content.")
    parser.add_argument("--interrupt-policy", choices=INTERRUPT_POLICIES, help="Speech interruption policy.")
    parser.add_argument("--skip-git-repo-check", action="store_true", help="Pass --skip-git-repo-check to the default `codex exec` command.")
    parser.add_argument("--auto-start-server", dest="auto_start_server", action="store_true", help="Auto-start the official websocket server.")
    parser.add_argument("--no-auto-start-server", dest="auto_start_server", action="store_false", help="Disable auto-start for the official websocket server.")
    parser.set_defaults(auto_start_server=None)

    for flag in (
        "speak_assistant_deltas",
        "speak_assistant_completed",
        "speak_reasoning_summary",
        "speak_status_announcements",
    ):
        cli_flag = flag.replace("_", "-")
        parser.add_argument(f"--{cli_flag}", dest=flag, action="store_true")
        parser.add_argument(f"--no-{cli_flag}", dest=flag, action="store_false")
        parser.set_defaults(**{flag: None})

    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Optional explicit Codex command after `--`, for example: -- codex exec --json ...",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = VoiceBridgeController(args)

    def _handle_signal(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    controller.start()
    try:
        exit_code = controller.run_codex()
    finally:
        controller.shutdown()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
