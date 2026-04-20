from __future__ import annotations

import json
import platform
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import sounddevice as sd
from mlx_audio.audio_io import write as audio_write
from mlx_audio.tts.models.base import adjust_speed
from mlx_audio.tts.utils import load_model

from tools.tts_common import DEFAULT_APPLE_MODEL, DEFAULT_APPLE_VOICE


class WorkerState:
    def __init__(self) -> None:
        self.model = None
        self.model_name = DEFAULT_APPLE_MODEL
        self.voice = DEFAULT_APPLE_VOICE
        self.speed = 1.0
        self.cfg = 1.5
        self.steps: Optional[int] = None
        self.max_tokens = 1_200
        self._active_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._playback_process: Optional[subprocess.Popen] = None
        self._temp_output_path: Optional[Path] = None

    def send(self, payload: Dict[str, Any]) -> None:
        with self._write_lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
            sys.stdout.flush()

    def ensure_model(self) -> None:
        if self.model is None:
            self.model = load_model(self.model_name)

    def load(self, payload: Dict[str, Any]) -> None:
        model_name = str(payload.get("model") or self.model_name)
        self.model_name = model_name
        self.voice = str(payload.get("voice") or self.voice)
        self.speed = float(payload.get("speed") or self.speed)
        cfg = payload.get("cfg")
        if cfg is not None:
            self.cfg = float(cfg)
        self.steps = payload.get("steps")
        self.ensure_model()
        self.send({"type": "loaded", "model": self.model_name, "voice": self.voice, "speed": self.speed})

    def set_voice(self, payload: Dict[str, Any]) -> None:
        voice = str(payload.get("voice") or "").strip()
        self.voice = voice or DEFAULT_APPLE_VOICE
        self.send({"type": "voice_updated", "voice": self.voice})

    def set_speed(self, payload: Dict[str, Any]) -> None:
        speed = float(payload.get("speed") or 1.0)
        self.speed = max(0.6, min(2.0, speed))
        self.send({"type": "speed_updated", "speed": self.speed})

    def _cleanup_temp_output(self) -> None:
        if self._temp_output_path is None:
            return
        self._temp_output_path.unlink(missing_ok=True)
        self._temp_output_path = None

    def _stop_playback(self) -> None:
        process = self._playback_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._playback_process = None
        self._cleanup_temp_output()

    def _play_audio(self, audio: np.ndarray, sample_rate: int, request_id: str) -> bool:
        if platform.system() == "Darwin":
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_path = Path(temp_file.name)
            self._temp_output_path = temp_path
            audio_write(str(temp_path), audio, sample_rate, format="wav")
            self._playback_process = subprocess.Popen(
                ["/usr/bin/afplay", str(temp_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                while self._playback_process.poll() is None:
                    if self._stop_event.is_set():
                        self._stop_playback()
                        return True
                    time.sleep(0.05)
            finally:
                self._playback_process = None
                self._cleanup_temp_output()
            return False

        sd.play(audio, sample_rate)
        while sd.get_stream().active:
            if self._stop_event.is_set():
                sd.stop()
                return True
            time.sleep(0.05)
        return False

    def _speak_task(self, text: str, request_id: str) -> None:
        try:
            self.ensure_model()
            self.send({"type": "speech_started", "request_id": request_id})
            kwargs = {
                "text": text,
                "voice": self.voice,
                "cfg_scale": self.cfg,
                "max_tokens": self.max_tokens,
                "verbose": False,
            }
            if self.steps is not None:
                kwargs["ddpm_steps"] = self.steps

            assert self.model is not None
            audio_chunks = []
            sample_rate: Optional[int] = None
            for result in self.model.generate(**kwargs):
                if self._stop_event.is_set():
                    self.send({"type": "speech_done", "request_id": request_id, "interrupted": True})
                    return
                audio = np.asarray(result.audio, dtype=np.float32)
                if audio.size == 0:
                    continue
                audio_chunks.append(audio)
                sample_rate = int(result.sample_rate)

            if not audio_chunks or sample_rate is None:
                self.send({"type": "speech_done", "request_id": request_id, "interrupted": False, "spoke_audio": False})
                return

            joined = np.concatenate(audio_chunks, axis=0)
            if abs(self.speed - 1.0) > 1e-3:
                joined = np.asarray(adjust_speed(joined, self.speed), dtype=np.float32)

            interrupted = self._play_audio(joined, sample_rate, request_id)
            self.send({"type": "speech_done", "request_id": request_id, "interrupted": interrupted, "spoke_audio": True})
        except BaseException as exc:  # pragma: no cover - runtime surface
            self.send({"type": "error", "request_id": request_id, "message": str(exc)})
        finally:
            self._stop_event.clear()
            self._active_thread = None

    def speak(self, payload: Dict[str, Any]) -> None:
        request_id = str(payload["request_id"])
        text = str(payload["text"])
        if self._active_thread is not None and self._active_thread.is_alive():
            self.send({"type": "error", "request_id": request_id, "message": "speech already in progress"})
            return
        self._stop_event.clear()
        self._active_thread = threading.Thread(target=self._speak_task, args=(text, request_id), daemon=True)
        self._active_thread.start()

    def stop(self, payload: Dict[str, Any]) -> None:
        self._stop_event.set()
        self._stop_playback()
        try:
            sd.stop()
        except Exception:
            pass
        self.send({"type": "stop_ack", "request_id": payload.get("request_id")})

    def shutdown(self) -> None:
        self._stop_event.set()
        self._stop_playback()
        try:
            sd.stop()
        except Exception:
            pass
        self.send({"type": "shutdown"})


def main() -> None:
    state = WorkerState()
    state.send({"type": "ready"})
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        payload = json.loads(raw_line)
        command = payload.get("command")
        if command == "load":
            state.load(payload)
        elif command == "set_voice":
            state.set_voice(payload)
        elif command == "set_speed":
            state.set_speed(payload)
        elif command == "speak":
            state.speak(payload)
        elif command in {"stop", "skip"}:
            state.stop(payload)
        elif command == "shutdown":
            state.shutdown()
            return
        else:
            state.send({"type": "error", "message": f"unknown command: {command}"})


if __name__ == "__main__":
    main()
