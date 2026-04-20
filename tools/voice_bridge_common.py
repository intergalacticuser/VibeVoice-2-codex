from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tts_common import DEFAULT_APPLE_VOICE, DEFAULT_HOST, DEFAULT_PORT, REPO_ROOT

BRIDGE_HOME = REPO_ROOT / ".codex-home" / "voice-bridge"
SETTINGS_PATH = BRIDGE_HOME / "settings.json"
SESSION_PATH = BRIDGE_HOME / "session.json"
DESKTOP_MIRROR_ROOT = BRIDGE_HOME / "desktop-mirror"

CHANNEL_ASSISTANT_DELTAS = "assistant_deltas"
CHANNEL_ASSISTANT_COMPLETED = "assistant_completed"
CHANNEL_REASONING_SUMMARY = "reasoning_summary"
CHANNEL_STATUS = "status_announcements"
CHANNELS = (
    CHANNEL_ASSISTANT_DELTAS,
    CHANNEL_ASSISTANT_COMPLETED,
    CHANNEL_REASONING_SUMMARY,
    CHANNEL_STATUS,
)
INTERRUPT_POLICIES = ("finish_current", "interrupt_latest", "manual")
COMMENTARY_VOICE_MODES = ("original_text", "english_status_only")
DESKTOP_TEXT_MODES = ("original_text", "translate_to_english", "english_reports_only")
DESKTOP_SPEECH_MODES = ("live_fast", "english_full", "status_only")
DESKTOP_SPEECH_MODE_ALIASES = {
    "everything": "live_fast",
    "live_fast": "live_fast",
    "english_only": "english_full",
    "english_full": "english_full",
    "status_only": "status_only",
}


@dataclass(frozen=True)
class SpeechChunkConfig:
    emit_max_chars: int
    buffer_min_chars: int
    buffer_max_chars: int
    merge_max_chars: int


FAST_CHUNK_CONFIG = SpeechChunkConfig(
    emit_max_chars=110,
    buffer_min_chars=12,
    buffer_max_chars=110,
    merge_max_chars=140,
)
BALANCED_CHUNK_CONFIG = SpeechChunkConfig(
    emit_max_chars=140,
    buffer_min_chars=24,
    buffer_max_chars=140,
    merge_max_chars=220,
)


def ensure_bridge_home() -> Path:
    BRIDGE_HOME.mkdir(parents=True, exist_ok=True)
    return BRIDGE_HOME


def truncate_preview(text: str, max_chars: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}..."


def allocate_control_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class BridgeSettings:
    backend: str = "apple"
    voice: str = ""
    speed: float = 1.0
    cfg: float = 1.5
    steps: Optional[int] = None
    speak_assistant_deltas: bool = True
    speak_assistant_completed: bool = True
    speak_reasoning_summary: bool = True
    speak_status_announcements: bool = True
    interrupt_policy: str = "finish_current"
    desktop_speech_mode: str = "english_full"
    commentary_voice_mode: str = "original_text"
    desktop_text_mode: str = "english_reports_only"
    muted: bool = False
    read_through: bool = False
    panel_geometry: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model: Optional[str] = None
    auto_start_server: bool = False
    speak_code: bool = False

    def effective_voice(self) -> Optional[str]:
        if self.voice.strip():
            return self.voice.strip()
        if self.backend == "apple":
            return DEFAULT_APPLE_VOICE
        return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BridgeSettings":
        defaults = cls()
        allowed = defaults.to_dict().keys()
        values = {key: payload[key] for key in allowed if key in payload}
        settings = cls(**values)
        sync_desktop_speech_mode(settings)
        return settings


def infer_desktop_speech_mode(settings: BridgeSettings) -> str:
    if (
        settings.commentary_voice_mode == "english_status_only"
        and settings.desktop_text_mode == "english_reports_only"
    ):
        return "status_only"
    if settings.desktop_text_mode == "translate_to_english":
        return "english_full"
    return "live_fast"


def normalize_desktop_speech_mode(mode: str) -> Optional[str]:
    return DESKTOP_SPEECH_MODE_ALIASES.get(str(mode or "").strip())


def apply_desktop_speech_mode(settings: BridgeSettings, mode: str) -> None:
    normalized_mode = normalize_desktop_speech_mode(mode)
    if normalized_mode is None:
        normalized_mode = "english_full"
    mode = normalized_mode
    if mode == "status_only":
        settings.desktop_speech_mode = "status_only"
        settings.commentary_voice_mode = "english_status_only"
        settings.desktop_text_mode = "english_reports_only"
        return
    if mode == "english_full":
        settings.desktop_speech_mode = "english_full"
        settings.commentary_voice_mode = "original_text"
        settings.desktop_text_mode = "translate_to_english"
        return
    settings.desktop_speech_mode = "live_fast"
    settings.commentary_voice_mode = "original_text"
    settings.desktop_text_mode = "original_text"


def sync_desktop_speech_mode(settings: BridgeSettings) -> None:
    mode = normalize_desktop_speech_mode(settings.desktop_speech_mode)
    if mode is None:
        mode = infer_desktop_speech_mode(settings)
    apply_desktop_speech_mode(settings, mode)


def chunk_config_for_settings(settings: BridgeSettings) -> SpeechChunkConfig:
    mode = normalize_desktop_speech_mode(settings.desktop_speech_mode) or infer_desktop_speech_mode(settings)
    if mode == "live_fast":
        return FAST_CHUNK_CONFIG
    return BALANCED_CHUNK_CONFIG


@dataclass
class SpeechItem:
    id: str
    channel: str
    text: str
    source_item_id: str
    turn_id: str
    group_key: str
    interruptible: bool = True

    def preview(self) -> str:
        return truncate_preview(self.text)


@dataclass
class BridgeSnapshot:
    settings: Dict[str, Any]
    current: Optional[Dict[str, Any]]
    queue: List[Dict[str, Any]]
    backend_ready: bool
    backend_error: Optional[str]
    codex_running: bool
    control_url: str
    pid: int


def serialize_item(item: Optional[SpeechItem]) -> Optional[Dict[str, Any]]:
    if item is None:
        return None
    return {
        "id": item.id,
        "channel": item.channel,
        "text": item.text,
        "preview": item.preview(),
        "source_item_id": item.source_item_id,
        "turn_id": item.turn_id,
        "group_key": item.group_key,
        "interruptible": item.interruptible,
    }


def load_settings() -> BridgeSettings:
    ensure_bridge_home()
    if not SETTINGS_PATH.exists():
        return BridgeSettings()
    payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return BridgeSettings.from_dict(payload)


def save_settings(settings: BridgeSettings) -> None:
    ensure_bridge_home()
    SETTINGS_PATH.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")


def write_session(control_url: str, pid: int) -> None:
    ensure_bridge_home()
    payload = {"control_url": control_url, "pid": pid}
    SESSION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_session() -> Dict[str, Any]:
    if not SESSION_PATH.exists():
        raise FileNotFoundError("No active voice-bridge session file found.")
    return json.loads(SESSION_PATH.read_text(encoding="utf-8"))


def clear_session() -> None:
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
