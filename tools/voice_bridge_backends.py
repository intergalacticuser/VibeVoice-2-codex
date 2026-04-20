from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from tools.tts_common import launch_server, repo_python, repo_python_for_backend, wait_for_server
from tools.voice_bridge_common import BridgeSettings


class BaseSpeechBackend:
    def speak(self, text: str) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def set_voice(self, voice: Optional[str]) -> None:
        raise NotImplementedError

    def set_speed(self, speed: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class AppleSpeechBackend(BaseSpeechBackend):
    def __init__(self, settings: BridgeSettings, model_name: str) -> None:
        self.settings = settings
        self.model_name = model_name
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._current_request_id: Optional[str] = None
        self._request_lock = threading.Lock()
        self._start_worker()

    def _clear_event_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _start_worker(self) -> None:
        self._clear_event_queue()
        self._proc = subprocess.Popen(
            [str(repo_python_for_backend("apple")), "-m", "tools.mlx_vibevoice_worker"],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._wait_for("ready", timeout=30.0)
        self._send(
            {
                "command": "load",
                "model": self.model_name,
                "voice": self.settings.effective_voice(),
                "speed": self.settings.speed,
                "cfg": self.settings.cfg,
                "steps": self.settings.steps,
            }
        )
        self._wait_for("loaded", timeout=180.0)

    def _restart_worker(self) -> None:
        self._shutdown_worker()
        self._start_worker()

    def _shutdown_worker(self) -> None:
        if self._proc is None:
            return
        try:
            self._send({"command": "shutdown"})
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None

    def _pump_stdout(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._queue.put(payload)

    def _pump_stderr(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("Apple MLX worker exited before the command could be sent.")
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("Apple MLX worker is no longer accepting commands.") from exc

    def _wait_for(self, expected_type: str, request_id: Optional[str] = None, timeout: float = 60.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            if self._proc.poll() is not None:
                raise RuntimeError(f"Apple MLX worker exited unexpectedly while waiting for `{expected_type}`.")
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out while waiting for Apple worker event `{expected_type}`.")
            try:
                payload = self._queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if payload.get("type") == "error":
                raise RuntimeError(payload.get("message", "apple worker error"))
            if payload.get("type") != expected_type:
                continue
            if request_id is not None and payload.get("request_id") != request_id:
                continue
            return payload

    def _speech_timeout_for_text(self, text: str) -> float:
        compact = " ".join(text.split())
        if len(compact) <= 24:
            return 18.0
        if len(compact) <= 80:
            return 35.0
        if len(compact) <= 160:
            return 55.0
        return 75.0

    def speak(self, text: str) -> bool:
        request_id = str(uuid.uuid4())
        with self._request_lock:
            self._current_request_id = request_id
            self._send({"command": "speak", "request_id": request_id, "text": text})
            self._wait_for("speech_started", request_id=request_id, timeout=30.0)
            try:
                result = self._wait_for(
                    "speech_done",
                    request_id=request_id,
                    timeout=self._speech_timeout_for_text(text),
                )
            except TimeoutError as exc:
                self._restart_worker()
                self._current_request_id = None
                raise RuntimeError("Apple MLX worker timed out on a speech item and was restarted.") from exc
            self._current_request_id = None
            return bool(result.get("interrupted"))

    def stop(self) -> None:
        request_id = self._current_request_id or str(uuid.uuid4())
        try:
            self._send({"command": "stop", "request_id": request_id})
        except BrokenPipeError:
            return

    def set_voice(self, voice: Optional[str]) -> None:
        self._send({"command": "set_voice", "voice": voice or ""})
        self._wait_for("voice_updated", timeout=10.0)

    def set_speed(self, speed: float) -> None:
        self._send({"command": "set_speed", "speed": speed})
        self._wait_for("speed_updated", timeout=10.0)

    def close(self) -> None:
        self._shutdown_worker()


class OfficialSpeechBackend(BaseSpeechBackend):
    def __init__(self, settings: BridgeSettings, model_name: str) -> None:
        self.settings = settings
        self.model_name = model_name
        self._current_process: Optional[subprocess.Popen] = None
        self._stop_requested = False
        self._server_process: Optional[subprocess.Popen] = None
        self._ensure_server()

    def _ensure_server(self) -> None:
        if wait_for_server(host=self.settings.host, port=self.settings.port, timeout=1.0):
            return
        if not self.settings.auto_start_server:
            raise RuntimeError(
                f"Official VibeVoice server is not reachable at http://{self.settings.host}:{self.settings.port}"
            )
        self._server_process = launch_server(
            host=self.settings.host,
            port=self.settings.port,
            model=self.model_name,
        )
        if not wait_for_server(host=self.settings.host, port=self.settings.port, timeout=600.0):
            raise RuntimeError("Timed out while starting the official VibeVoice websocket server.")

    def speak(self, text: str) -> bool:
        self._stop_requested = False
        command = [
            str(repo_python()),
            "-m",
            "tools.vibevoice_speak",
            "--backend",
            "official",
            "--host",
            self.settings.host,
            "--port",
            str(self.settings.port),
            "--text",
            text,
            "--cfg",
            str(self.settings.cfg),
        ]
        if self.settings.effective_voice():
            command.extend(["--voice", str(self.settings.effective_voice())])
        if self.settings.steps is not None:
            command.extend(["--steps", str(self.settings.steps)])
        self._current_process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1])
        try:
            self._current_process.wait()
        finally:
            self._current_process = None
        return self._stop_requested

    def stop(self) -> None:
        self._stop_requested = True
        process = self._current_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def set_voice(self, voice: Optional[str]) -> None:
        self.settings.voice = voice or ""

    def set_speed(self, speed: float) -> None:
        self.settings.speed = speed

    def close(self) -> None:
        self.stop()
        if self._server_process is not None and self._server_process.poll() is None:
            self._server_process.terminate()
            self._server_process.wait(timeout=10)


def create_backend(settings: BridgeSettings, model_name: str) -> BaseSpeechBackend:
    if settings.backend == "apple":
        return AppleSpeechBackend(settings, model_name=model_name)
    return OfficialSpeechBackend(settings, model_name=model_name)
