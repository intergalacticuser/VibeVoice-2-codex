from __future__ import annotations

import os
import platform
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_VENV = REPO_ROOT / ".venv"
APPLE_VENV = REPO_ROOT / ".venv-mlx"
DEFAULT_MODEL = "microsoft/VibeVoice-Realtime-0.5B"
DEFAULT_APPLE_MODEL = "mlx-community/VibeVoice-Realtime-0.5B-4bit"
DEFAULT_APPLE_VOICE = "en-Emma_woman"
DEFAULT_BACKEND = "auto"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_DEVICE = "mps"

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
URL_RE = re.compile(r"https?://\S+")
WHITESPACE_RE = re.compile(r"\s+")
CODE_SYMBOL_RE = re.compile(r"[{}[\]<>$#@~=|\\]")
SENTENCE_BOUNDARY_RE = re.compile(r"(?:[.!?](?:\s|$)|\n)")
CLAUSE_BOUNDARY_RE = re.compile(r"(?<=[,;:])\s+")


def repo_python() -> Path:
    return OFFICIAL_VENV / "bin" / "python"


def repo_python_for_backend(backend: str) -> Path:
    resolved = resolve_backend(backend)
    if resolved == "apple":
        return APPLE_VENV / "bin" / "python"
    return OFFICIAL_VENV / "bin" / "python"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def default_backend() -> str:
    return "apple" if is_macos() else "official"


def resolve_backend(backend: str) -> str:
    if backend == "auto":
        return default_backend()
    if backend not in {"official", "apple"}:
        raise ValueError(f"Unsupported backend: {backend}")
    return backend


def default_model_for_backend(backend: str) -> str:
    resolved = resolve_backend(backend)
    if resolved == "apple":
        return DEFAULT_APPLE_MODEL
    return DEFAULT_MODEL


def resolve_model_name(backend: str, explicit_model: Optional[str]) -> str:
    if explicit_model:
        return explicit_model
    backend_env = "VIBEVOICE_APPLE_MODEL" if resolve_backend(backend) == "apple" else "VIBEVOICE_OFFICIAL_MODEL"
    return os.environ.get("VIBEVOICE_MODEL") or os.environ.get(backend_env) or default_model_for_backend(backend)


def server_http_base(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}"


def server_config_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"{server_http_base(host, port)}/config"


def build_stream_url(
    text: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    voice: Optional[str] = None,
    cfg: float = 1.5,
    steps: Optional[int] = None,
) -> str:
    params = {"text": text, "cfg": str(cfg)}
    if voice:
        params["voice"] = voice
    if steps:
        params["steps"] = str(steps)
    query = urllib.parse.urlencode(params)
    return f"ws://{host}:{port}/stream?{query}"


def is_server_healthy(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(server_config_url(host, port), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def wait_for_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 600.0,
    poll_interval: float = 1.0,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_server_healthy(host=host, port=port):
            return True
        time.sleep(poll_interval)
    return False


def launch_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    python_bin: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> subprocess.Popen:
    python_bin = python_bin or repo_python()
    log_path = log_path or (REPO_ROOT / "logs" / "vibevoice-server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    cmd = [
        str(python_bin),
        "demo/vibevoice_realtime_demo.py",
        "--port",
        str(port),
        "--model_path",
        model,
        "--device",
        device,
    ]
    return subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=False,
    )


def default_device() -> str:
    if is_macos():
        return "mps"
    return "cuda"


class AudioSink:
    def __init__(
        self,
        *,
        play_audio: bool = True,
        output_path: Optional[Path] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.play_audio = play_audio
        self.output_path = output_path
        self.sample_rate = sample_rate
        self._ffplay: Optional[subprocess.Popen] = None
        self._pcm = bytearray()

    def _ensure_player(self) -> None:
        if not self.play_audio or self._ffplay is not None:
            return
        self._ffplay = subprocess.Popen(
            [
                "ffplay",
                "-autoexit",
                "-nodisp",
                "-loglevel",
                "warning",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                "1",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, pcm_chunk: bytes) -> None:
        if not pcm_chunk:
            return
        self._pcm.extend(pcm_chunk)
        if not self.play_audio:
            return
        self._ensure_player()
        if self._ffplay is None or self._ffplay.stdin is None:
            raise RuntimeError("ffplay is not available for streaming playback.")
        self._ffplay.stdin.write(pcm_chunk)
        self._ffplay.stdin.flush()

    def close(self) -> None:
        if self._ffplay is not None and self._ffplay.stdin is not None:
            try:
                self._ffplay.stdin.close()
            except BrokenPipeError:
                pass
            self._ffplay.wait()
        if self.output_path:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(self.output_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(bytes(self._pcm))


def _humanize_inline_code(value: str) -> str:
    cleaned = value.strip().replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""
    if CODE_SYMBOL_RE.search(cleaned):
        return ""
    return cleaned


def _is_line_code_like(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("```", "$ ", "> ", ">>> ", "... ")):
        return True
    if stripped.count("|") >= 2 and stripped.startswith("|"):
        return True
    symbol_count = sum(1 for char in stripped if char in "{}[]<>$#@~=|\\")
    return symbol_count >= 3


def normalize_tts_text(text: str, *, speak_code: bool = False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = URL_RE.sub(" link ", text)
    if not speak_code:
        text = CODE_FENCE_RE.sub(" ", text)
        text = INLINE_CODE_RE.sub(" ", text)
    else:
        text = INLINE_CODE_RE.sub(lambda match: _humanize_inline_code(match.group(1)), text)

    kept_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept_lines.append("")
            continue
        if stripped.startswith(("#", "---")):
            continue
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+\.\s+", "", line)
        if not speak_code and _is_line_code_like(line):
            continue
        kept_lines.append(line)

    text = "\n".join(kept_lines)
    text = text.replace("`", " ")
    text = text.replace("*", " ")
    text = text.replace("_", " ")
    text = text.replace("|", " ")
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def split_text_for_tts(
    text: str,
    *,
    max_chars: int = 160,
    normalized: bool = False,
    speak_code: bool = False,
) -> List[str]:
    prepared = text if normalized else normalize_tts_text(text, speak_code=speak_code)
    if not prepared:
        return []

    rough_parts = re.split(r"(?<=[.!?])\s+", prepared)
    chunks: List[str] = []
    current = ""
    for part in rough_parts:
        part = part.strip()
        if not part:
            continue
        if not current:
            current = part
            continue
        combined = f"{current} {part}".strip()
        if len(combined) <= max_chars:
            current = combined
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)

    final_chunks: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final_chunks.append(chunk)
            continue

        clause_parts = [part.strip() for part in CLAUSE_BOUNDARY_RE.split(chunk) if part.strip()]
        if len(clause_parts) > 1:
            clause_acc = ""
            for part in clause_parts:
                candidate = f"{clause_acc} {part}".strip() if clause_acc else part
                if clause_acc and len(candidate) > max_chars:
                    final_chunks.append(clause_acc)
                    clause_acc = part
                else:
                    clause_acc = candidate
            if clause_acc:
                chunk = clause_acc
            else:
                continue
            if len(chunk) <= max_chars:
                final_chunks.append(chunk)
                continue

        words = chunk.split()
        acc = ""
        for word in words:
            candidate = f"{acc} {word}".strip()
            if acc and len(candidate) > max_chars:
                final_chunks.append(acc)
                acc = word
            else:
                acc = candidate
        if acc:
            final_chunks.append(acc)
    return [chunk for chunk in final_chunks if chunk]


class IncrementalSentenceBuffer:
    def __init__(self, min_chars: int = 24, max_chars: int = 160, speak_code: bool = False) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.speak_code = speak_code
        self._raw_buffer = ""

    def _flush_chunk(self, raw_chunk: str) -> List[str]:
        normalized = normalize_tts_text(raw_chunk, speak_code=self.speak_code)
        return split_text_for_tts(normalized, max_chars=self.max_chars, normalized=True)

    def push(self, delta: str) -> List[str]:
        if not delta:
            return []
        self._raw_buffer += delta
        ready: List[str] = []

        while True:
            if len(self._raw_buffer) < self.min_chars:
                break

            boundary = None
            for match in SENTENCE_BOUNDARY_RE.finditer(self._raw_buffer):
                boundary = match.end()

            if boundary is None and len(self._raw_buffer) < self.max_chars * 2:
                break

            if boundary is None:
                boundary = self.max_chars

            raw_chunk = self._raw_buffer[:boundary]
            self._raw_buffer = self._raw_buffer[boundary:]
            ready.extend(self._flush_chunk(raw_chunk))

        return ready

    def flush(self) -> List[str]:
        if not self._raw_buffer.strip():
            self._raw_buffer = ""
            return []
        raw_chunk = self._raw_buffer
        self._raw_buffer = ""
        return self._flush_chunk(raw_chunk)


def flatten(items: Iterable[Iterable[str]]) -> List[str]:
    flattened: List[str] = []
    for item in items:
        flattened.extend(item)
    return flattened
