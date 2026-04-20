from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from tools.tts_common import IncrementalSentenceBuffer, split_text_for_tts
from tools.voice_bridge_common import (
    CHANNEL_ASSISTANT_COMPLETED,
    CHANNEL_ASSISTANT_DELTAS,
    CHANNEL_REASONING_SUMMARY,
    CHANNEL_STATUS,
    BridgeSettings,
    chunk_config_for_settings,
)

VISIBLE_DELTA_METHODS = {"item/agentMessage/delta", "item/plan/delta"}
SAFE_SUMMARY_DELTA_METHODS = {"item/reasoning/summaryTextDelta"}

EmitCallback = Callable[[str, str, str, str, str, bool], None]
InterruptCallback = Callable[[str], None]


def normalize_item_type(value: Any) -> str:
    raw = str(value or "")
    lowered = raw.lower().replace("/", "").replace(".", "").replace("_", "")
    if lowered == "agentmessage":
        return "agentMessage"
    if lowered == "plan":
        return "plan"
    if lowered == "reasoning":
        return "reasoning"
    return raw


class CodexSpeechRouter:
    def __init__(
        self,
        *,
        settings_provider: Callable[[], BridgeSettings],
        emit: EmitCallback,
        interrupt: InterruptCallback,
    ) -> None:
        self._settings_provider = settings_provider
        self._emit = emit
        self._interrupt = interrupt
        self._assistant_buffers: Dict[str, IncrementalSentenceBuffer] = {}
        self._reasoning_buffers: Dict[Tuple[str, int], IncrementalSentenceBuffer] = {}
        self._visible_delta_items: set[str] = set()
        self._reasoning_delta_keys: set[Tuple[str, int]] = set()
        self._completed_items: set[str] = set()
        self._turn_status: Dict[str, Dict[str, bool]] = {}
        self._started_groups: set[str] = set()
        self._superseded_groups: set[str] = set()
        self._active_group_key: Optional[str] = None
        self._current_turn_id = "turn-unknown"

    def reset(self) -> None:
        self._assistant_buffers.clear()
        self._reasoning_buffers.clear()
        self._visible_delta_items.clear()
        self._reasoning_delta_keys.clear()
        self._completed_items.clear()
        self._turn_status.clear()
        self._started_groups.clear()
        self._superseded_groups.clear()
        self._active_group_key = None
        self._current_turn_id = "turn-unknown"

    @property
    def settings(self) -> BridgeSettings:
        return self._settings_provider()

    def _status_flags(self, turn_id: str) -> Dict[str, bool]:
        flags = self._turn_status.get(turn_id)
        if flags is None:
            flags = {"working": False, "planning": False, "writing": False, "plan": False, "tool": False, "completed": False}
            self._turn_status[turn_id] = flags
        return flags

    def _group_key(self, turn_id: str, item_type: str, item_id: str) -> str:
        return f"{turn_id}:{item_type}:{item_id}"

    def _group_parts(self, group_key: str) -> Tuple[str, str, str]:
        turn_id, item_type, item_id = group_key.split(":", 2)
        return turn_id, item_type, item_id

    def _active_interrupt_mode(self) -> bool:
        settings = self.settings
        return settings.interrupt_policy == "interrupt_latest" and not settings.read_through

    def _maybe_interrupt(self, group_key: str) -> None:
        if not self._active_interrupt_mode():
            return
        self._interrupt(group_key)

    def _emit_chunks(
        self,
        channel: str,
        text: str,
        source_item_id: str,
        turn_id: str,
        group_key: str,
        interruptible: bool = True,
    ) -> None:
        settings = self.settings
        chunk_config = chunk_config_for_settings(settings)
        if settings.muted:
            return
        if channel == CHANNEL_STATUS and not settings.speak_status_announcements:
            return
        if channel == CHANNEL_ASSISTANT_DELTAS and not settings.speak_assistant_deltas:
            return
        if channel == CHANNEL_ASSISTANT_COMPLETED and not settings.speak_assistant_completed:
            return
        if channel == CHANNEL_REASONING_SUMMARY and not settings.speak_reasoning_summary:
            return
        for chunk in split_text_for_tts(text, max_chars=chunk_config.emit_max_chars, speak_code=settings.speak_code):
            self._emit(channel, chunk, source_item_id, turn_id, group_key, interruptible)

    def _emit_status_once(self, turn_id: str, key: str, text: str) -> None:
        flags = self._status_flags(turn_id)
        if flags[key]:
            return
        flags[key] = True
        self._emit_chunks(CHANNEL_STATUS, text, f"status-{key}", turn_id, f"{turn_id}:status:{key}", interruptible=False)

    def _on_turn_started(self, turn_id: str) -> None:
        self._current_turn_id = turn_id
        if self.settings.speak_status_announcements:
            self._emit_status_once(turn_id, "working", "Working.")

    def _drop_stale_buffers(self, keep_group_key: str, item_type: str) -> None:
        visible_types = {"agentMessage", "plan"}
        if item_type in visible_types:
            self._assistant_buffers = {
                group_key: buffer
                for group_key, buffer in self._assistant_buffers.items()
                if group_key == keep_group_key
            }
        else:
            self._assistant_buffers.clear()

        if item_type == "reasoning":
            self._reasoning_buffers = {
                key: buffer
                for key, buffer in self._reasoning_buffers.items()
                if key[0] == keep_group_key
            }
            self._reasoning_delta_keys = {
                key
                for key in self._reasoning_delta_keys
                if key[0] == keep_group_key
            }
        else:
            self._reasoning_buffers.clear()
            self._reasoning_delta_keys.clear()

    def _register_group(self, group_key: str, item_type: str) -> None:
        if self._active_group_key == group_key:
            return
        if self._active_interrupt_mode():
            if self._active_group_key:
                self._superseded_groups.add(self._active_group_key)
            self._drop_stale_buffers(group_key, item_type)
            self._superseded_groups.discard(group_key)
        self._active_group_key = group_key

    def _ensure_group_started(self, item_type: str, item_id: str, turn_id: str) -> str:
        group_key = self._group_key(turn_id, item_type, item_id)
        if group_key in self._started_groups:
            return group_key
        self._started_groups.add(group_key)
        self._handle_item_started({"id": item_id, "type": item_type}, turn_id)
        return group_key

    def _is_superseded(self, group_key: str) -> bool:
        return self._active_interrupt_mode() and group_key in self._superseded_groups

    def _handle_item_started(self, item: Dict[str, Any], turn_id: str) -> None:
        item_type = normalize_item_type(item.get("type"))
        item_id = str(item.get("id") or f"{item_type}-unknown")
        group_key = self._group_key(turn_id, item_type, item_id)
        lowered = item_type.lower()
        relevant = item_type in {"agentMessage", "plan", "reasoning"} or "command" in lowered

        if relevant:
            self._register_group(group_key, item_type)
            self._maybe_interrupt(group_key)

        if lowered == "reasoning":
            self._emit_status_once(turn_id, "planning", "Planning.")
        elif lowered == "agentmessage":
            self._emit_status_once(turn_id, "writing", "Writing answer.")
        elif lowered == "plan":
            self._emit_status_once(turn_id, "plan", "Reading plan.")
        elif "command" in lowered:
            self._emit_status_once(turn_id, "tool", "Running tool.")

    def _flush_assistant_buffer(self, group_key: str, item_id: str, turn_id: str) -> None:
        buffer = self._assistant_buffers.pop(group_key, None)
        if buffer is None:
            return
        for chunk in buffer.flush():
            self._emit(CHANNEL_ASSISTANT_DELTAS, chunk, item_id, turn_id, group_key, True)

    def _flush_reasoning_buffers(self, group_key: str, item_id: str, turn_id: str) -> None:
        for key in sorted(list(self._reasoning_buffers.keys())):
            if key[0] != group_key:
                continue
            buffer = self._reasoning_buffers.pop(key)
            for chunk in buffer.flush():
                self._emit(CHANNEL_REASONING_SUMMARY, chunk, item_id, turn_id, group_key, True)

    def _handle_completed_item(self, item: Dict[str, Any], turn_id: str) -> None:
        item_type = normalize_item_type(item.get("type"))
        item_id = str(item.get("id") or f"{item_type}-unknown")
        group_key = self._group_key(turn_id, item_type, item_id)
        if group_key in self._completed_items:
            return
        self._completed_items.add(group_key)

        if self._is_superseded(group_key):
            self._assistant_buffers.pop(group_key, None)
            for key in list(self._reasoning_buffers.keys()):
                if key[0] == group_key:
                    self._reasoning_buffers.pop(key, None)
            self._reasoning_delta_keys = {
                key
                for key in self._reasoning_delta_keys
                if key[0] != group_key
            }
            return

        if item_type in {"agentMessage", "plan"}:
            if self.settings.speak_assistant_deltas:
                self._flush_assistant_buffer(group_key, item_id, turn_id)
            if not self.settings.speak_assistant_completed:
                return
            if self.settings.speak_assistant_deltas and group_key in self._visible_delta_items:
                return
            text = str(item.get("text") or "")
            self._emit_chunks(CHANNEL_ASSISTANT_COMPLETED, text, item_id, turn_id, group_key)
            return

        if item_type == "reasoning":
            if self.settings.speak_reasoning_summary:
                self._flush_reasoning_buffers(group_key, item_id, turn_id)
            if self.settings.speak_reasoning_summary and not any(key[0] == group_key for key in self._reasoning_delta_keys):
                for summary_text in item.get("summary") or []:
                    self._emit_chunks(CHANNEL_REASONING_SUMMARY, str(summary_text), item_id, turn_id, group_key)

    def _resolve_turn_id(self, payload: Dict[str, Any], default: Optional[str] = None) -> str:
        if default:
            return default
        if isinstance(payload.get("turnId"), str) and payload["turnId"]:
            return str(payload["turnId"])
        params = payload.get("params")
        if isinstance(params, dict) and isinstance(params.get("turnId"), str) and params["turnId"]:
            return str(params["turnId"])
        return self._current_turn_id

    def handle_payload(self, payload: Dict[str, Any]) -> None:
        method = payload.get("method")
        if isinstance(method, str):
            params = payload.get("params", {})
            turn_id = self._resolve_turn_id(params)
            self._current_turn_id = turn_id

            if method in VISIBLE_DELTA_METHODS:
                item_id = str(params.get("itemId") or "assistant-unknown")
                item_type = "plan" if method == "item/plan/delta" else "agentMessage"
                group_key = self._ensure_group_started(item_type, item_id, turn_id)
                if self._is_superseded(group_key):
                    return
                if not self.settings.speak_assistant_deltas:
                    return
                chunk_config = chunk_config_for_settings(self.settings)
                buffer = self._assistant_buffers.setdefault(
                    group_key,
                    IncrementalSentenceBuffer(
                        min_chars=chunk_config.buffer_min_chars,
                        max_chars=chunk_config.buffer_max_chars,
                        speak_code=self.settings.speak_code,
                    ),
                )
                for chunk in buffer.push(str(params.get("delta") or "")):
                    self._emit(CHANNEL_ASSISTANT_DELTAS, chunk, item_id, turn_id, group_key, True)
                self._visible_delta_items.add(group_key)
                return

            if method in SAFE_SUMMARY_DELTA_METHODS:
                item_id = str(params.get("itemId") or "reasoning-unknown")
                summary_index = int(params.get("summaryIndex") or 0)
                group_key = self._ensure_group_started("reasoning", item_id, turn_id)
                if self._is_superseded(group_key):
                    return
                if not self.settings.speak_reasoning_summary:
                    return
                key = (group_key, summary_index)
                chunk_config = chunk_config_for_settings(self.settings)
                buffer = self._reasoning_buffers.setdefault(
                    key,
                    IncrementalSentenceBuffer(
                        min_chars=chunk_config.buffer_min_chars,
                        max_chars=chunk_config.buffer_max_chars,
                        speak_code=self.settings.speak_code,
                    ),
                )
                for chunk in buffer.push(str(params.get("delta") or "")):
                    self._emit(CHANNEL_REASONING_SUMMARY, chunk, item_id, turn_id, group_key, True)
                self._reasoning_delta_keys.add(key)
                return

            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict):
                    self._handle_completed_item(item, turn_id)
                return

            if method == "item/started":
                item = params.get("item")
                if isinstance(item, dict):
                    item_type = normalize_item_type(item.get("type"))
                    item_id = str(item.get("id") or f"{item_type}-unknown")
                    self._started_groups.add(self._group_key(turn_id, item_type, item_id))
                    self._handle_item_started(item, turn_id)
                return

            return

        event_type = (payload.get("type") or "").lower().replace("/", "").replace(".", "").replace("_", "")
        turn_id = self._resolve_turn_id(payload)
        self._current_turn_id = turn_id

        if event_type == "turnstarted":
            self._on_turn_started(turn_id)
            return

        if event_type == "turncompleted":
            if self.settings.speak_status_announcements:
                self._emit_status_once(turn_id, "completed", "Turn completed.")
            return

        if event_type == "itemstarted":
            item = payload.get("item")
            if isinstance(item, dict):
                item_type = normalize_item_type(item.get("type"))
                item_id = str(item.get("id") or f"{item_type}-unknown")
                self._started_groups.add(self._group_key(turn_id, item_type, item_id))
                self._handle_item_started(item, turn_id)
            return

        if event_type == "itemcompleted":
            item = payload.get("item")
            if isinstance(item, dict):
                self._handle_completed_item(item, turn_id)

    def flush_all(self) -> None:
        for group_key, buffer in list(self._assistant_buffers.items()):
            if self._is_superseded(group_key):
                continue
            turn_id, _item_type, item_id = self._group_parts(group_key)
            for chunk in buffer.flush():
                self._emit(CHANNEL_ASSISTANT_DELTAS, chunk, item_id, turn_id, group_key, True)
        self._assistant_buffers.clear()

        for (group_key, _summary_index), buffer in list(self._reasoning_buffers.items()):
            if self._is_superseded(group_key):
                continue
            turn_id, _item_type, item_id = self._group_parts(group_key)
            for chunk in buffer.flush():
                self._emit(CHANNEL_REASONING_SUMMARY, chunk, item_id, turn_id, group_key, True)
        self._reasoning_buffers.clear()
