from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import select
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from tools.codex_vibevoice import VoiceBridgeController
from tools.text_translation import EnglishTextTranslator
from tools.tts_common import DEFAULT_BACKEND, DEFAULT_DEVICE, DEFAULT_HOST, DEFAULT_PORT
from tools.voice_bridge_common import DESKTOP_MIRROR_ROOT, ensure_bridge_home

CODEX_HOME = Path.home() / ".codex"
SESSION_ROOT = CODEX_HOME / "sessions"
GLOBAL_STATE_PATH = CODEX_HOME / ".codex-global-state.json"
STATE_DB_PATH = CODEX_HOME / "state_5.sqlite"
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
CYRILLIC_LETTER_RE = re.compile(r"[А-Яа-яЁё]")


def _fingerprint(*parts: str) -> str:
    joined = "\n".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _extract_message_text(content: Any) -> str:
    parts: List[str] = []
    if not isinstance(content, list):
        return ""
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"output_text", "input_text"}:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_summary_texts(summary: Any) -> List[str]:
    texts: List[str] = []
    if summary is None:
        return texts
    if isinstance(summary, str):
        text = summary.strip()
        if text:
            texts.append(text)
        return texts
    if isinstance(summary, dict):
        for key in ("text", "summary", "content", "value"):
            if key not in summary:
                continue
            texts.extend(_extract_summary_texts(summary.get(key)))
        return texts
    if isinstance(summary, list):
        for item in summary:
            texts.extend(_extract_summary_texts(item))
        return texts
    return texts


def _iter_candidate_session_files(thread_id: Optional[str]) -> Iterable[Path]:
    if thread_id:
        pattern = str(SESSION_ROOT / "**" / f"*{thread_id}.jsonl")
        for raw_path in sorted(glob.glob(pattern, recursive=True)):
            yield Path(raw_path)
        return

    for raw_path in sorted(glob.glob(str(SESSION_ROOT / "**" / "*.jsonl"), recursive=True)):
        yield Path(raw_path)


def _read_session_meta(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, raw_line in enumerate(handle):
                if index > 8:
                    break
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    return payload
    except FileNotFoundError:
        return {}
    return {}


def resolve_session_file(thread_id: Optional[str], cwd_filter: Optional[str]) -> Path:
    candidates: List[tuple[float, Path]] = []
    for path in _iter_candidate_session_files(thread_id):
        meta = _read_session_meta(path)
        if meta.get("originator") != "Codex Desktop":
            continue
        if cwd_filter and meta.get("cwd") != cwd_filter:
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        candidates.append((mtime, path))

    if not candidates:
        raise FileNotFoundError("No matching Codex Desktop session file was found.")

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def wait_for_session_file(
    thread_id: Optional[str],
    cwd_filter: Optional[str],
    stop_event: threading.Event,
    poll_interval: float = 0.05,
) -> Path:
    while not stop_event.is_set():
        try:
            return resolve_session_file(thread_id=thread_id, cwd_filter=cwd_filter)
        except FileNotFoundError:
            time.sleep(poll_interval)
    raise SystemExit(130)


def _is_probably_english(text: str) -> bool:
    latin = len(LATIN_LETTER_RE.findall(text))
    cyrillic = len(CYRILLIC_LETTER_RE.findall(text))
    if latin == 0:
        return cyrillic == 0
    if cyrillic == 0:
        return True
    return latin >= max(8, cyrillic * 2)


def _english_proxy_for_visible(phase: str, text: str) -> str:
    lowered = text.lower()
    normalized_phase = phase.strip().lower()

    if normalized_phase == "commentary":
        if any(token in lowered for token in ("plan", "outline", "next step", "next steps")):
            return "Planning the next steps for the task."
        if any(token in lowered for token in ("test", "tests", "pytest", "unittest", "verify", "verification", "check")):
            return "Running checks on the current work."
        if any(token in lowered for token in ("read", "review", "inspect", "search", "scan", "grep", "rg", "file")):
            return "Reviewing the current codebase now."
        if any(token in lowered for token in ("implement", "patch", "edit", "update", "fix")):
            return "Implementing changes in the workspace."
        if any(token in lowered for token in ("voice", "speech", "audio", "bridge")):
            return "Adjusting the voice bridge settings."
        return "Progress update from the active chat."

    if normalized_phase in {"final_answer", "final", "assistant"}:
        return "Answer ready."

    if normalized_phase == "plan":
        return "Plan ready."

    return "Progress update."


def _english_proxy_for_reasoning(_summaries: List[str]) -> str:
    return "Reasoning update."


def read_active_workspace_roots(path: Path = GLOBAL_STATE_PATH) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    roots = payload.get("active-workspace-roots")
    if not isinstance(roots, list):
        return []
    return [str(root).strip() for root in roots if str(root).strip()]


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    session_file: Path
    cwd: str
    title: str
    updated_at: int


def _connect_state_db(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(path)


def load_thread_records(db_path: Path = STATE_DB_PATH, cwd_roots: Optional[List[str]] = None) -> List[ThreadRecord]:
    if not db_path.exists():
        return []
    query = "select id, rollout_path, cwd, title, updated_at from threads where archived=0"
    params: List[str] = []
    if cwd_roots:
        placeholders = ",".join("?" for _ in cwd_roots)
        query += f" and cwd in ({placeholders})"
        params.extend(cwd_roots)
    query += " order by updated_at desc"

    records: List[ThreadRecord] = []
    conn = _connect_state_db(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        for thread_id, rollout_path, cwd, title, updated_at in cursor.fetchall():
            path = Path(str(rollout_path))
            if not path.exists():
                continue
            records.append(
                ThreadRecord(
                    thread_id=str(thread_id),
                    session_file=path,
                    cwd=str(cwd or ""),
                    title=str(title or thread_id),
                    updated_at=int(updated_at or 0),
                )
            )
    finally:
        conn.close()
    return records


def load_thread_record_by_id(thread_id: str, db_path: Path = STATE_DB_PATH) -> Optional[ThreadRecord]:
    matches = [record for record in load_thread_records(db_path=db_path) if record.thread_id == thread_id]
    return matches[0] if matches else None


def load_thread_record_by_session_file(session_file: Path, db_path: Path = STATE_DB_PATH) -> Optional[ThreadRecord]:
    target = str(session_file)
    matches = [record for record in load_thread_records(db_path=db_path) if str(record.session_file) == target]
    return matches[0] if matches else None


def select_active_thread(
    thread_records: List[ThreadRecord],
    active_workspace_roots: List[str],
    fallback_thread_id: Optional[str] = None,
) -> Optional[ThreadRecord]:
    if not thread_records:
        return None

    for root in active_workspace_roots:
        for record in thread_records:
            if record.cwd == root:
                return record

    if fallback_thread_id:
        for record in thread_records:
            if record.thread_id == fallback_thread_id:
                return record

    return thread_records[0]


def build_thread_record_from_session_file(
    session_file: Path,
    *,
    thread_id: Optional[str] = None,
    cwd: Optional[str] = None,
    title: Optional[str] = None,
) -> ThreadRecord:
    meta = _read_session_meta(session_file)
    resolved_thread_id = str(thread_id or meta.get("id") or session_file.stem)
    resolved_cwd = str(cwd or meta.get("cwd") or "")
    resolved_title = str(title or meta.get("title") or meta.get("name") or resolved_thread_id)
    try:
        updated_at = int(session_file.stat().st_mtime)
    except FileNotFoundError:
        updated_at = 0
    return ThreadRecord(
        thread_id=resolved_thread_id,
        session_file=session_file,
        cwd=resolved_cwd,
        title=resolved_title,
        updated_at=updated_at,
    )


class ActiveDesktopThreadResolver:
    def __init__(
        self,
        *,
        cwd_filter: Optional[str],
        fallback_thread_id: Optional[str],
        global_state_path: Path = GLOBAL_STATE_PATH,
        state_db_path: Path = STATE_DB_PATH,
    ) -> None:
        self.cwd_filter = cwd_filter
        self.fallback_thread_id = fallback_thread_id
        self.global_state_path = global_state_path
        self.state_db_path = state_db_path

    def resolve(self) -> Optional[ThreadRecord]:
        active_roots = [self.cwd_filter] if self.cwd_filter else read_active_workspace_roots(self.global_state_path)
        thread_records = load_thread_records(self.state_db_path, cwd_roots=active_roots or None)
        selection = select_active_thread(thread_records, active_roots, fallback_thread_id=self.fallback_thread_id)
        if selection is not None:
            return selection

        if self.fallback_thread_id:
            fallback = load_thread_record_by_id(self.fallback_thread_id, db_path=self.state_db_path)
            if fallback is not None:
                return fallback
            try:
                session_file = resolve_session_file(self.fallback_thread_id, self.cwd_filter)
            except FileNotFoundError:
                return None
            return build_thread_record_from_session_file(
                session_file,
                thread_id=self.fallback_thread_id,
                cwd=self.cwd_filter,
            )

        if self.cwd_filter:
            try:
                session_file = resolve_session_file(None, self.cwd_filter)
            except FileNotFoundError:
                return None
            return build_thread_record_from_session_file(session_file, cwd=self.cwd_filter)

        return None


class DesktopMirrorWriter:
    def __init__(self, root: Path = DESKTOP_MIRROR_ROOT) -> None:
        self.root = root
        self._lock = threading.Lock()

    def _path_for_thread(self, thread_id: str) -> Path:
        ensure_bridge_home()
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{thread_id}.jsonl"

    def append(
        self,
        *,
        thread_id: str,
        cwd: str,
        turn_id: str,
        phase: str,
        kind: str,
        text: str,
    ) -> None:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return
        payload = {
            "timestamp": time.time(),
            "thread_id": thread_id,
            "cwd": cwd,
            "turn_id": turn_id,
            "phase": phase,
            "kind": kind,
            "text": normalized,
        }
        path = self._path_for_thread(thread_id)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


@dataclass
class VisibleMessageState:
    item_id: str
    phase: str
    source_text: str = ""
    text: str = ""


class DesktopSessionBridgeAdapter:
    def __init__(
        self,
        controller: VoiceBridgeController,
        *,
        thread_id: str,
        cwd: str,
        mirror_writer: Optional[DesktopMirrorWriter] = None,
        english_translator: Optional[Any] = None,
    ) -> None:
        self.controller = controller
        self.router = controller.router
        self.thread_id = thread_id
        self.cwd = cwd
        self.mirror_writer = mirror_writer
        self.english_translator = english_translator
        self.current_turn_id = "desktop-turn"
        self._spoken_visible_keys: set[str] = set()
        self._spoken_summary_keys: set[str] = set()
        self._tool_started_keys: set[str] = set()
        self._visible_states: Dict[tuple[str, str], VisibleMessageState] = {}
        self._visible_counter = 0

    @property
    def settings(self):
        return self.controller.settings

    def _write_mirror(self, *, turn_id: str, phase: str, kind: str, text: str, emit: bool) -> None:
        if not emit or self.mirror_writer is None:
            return
        self.mirror_writer.append(
            thread_id=self.thread_id,
            cwd=self.cwd,
            turn_id=turn_id,
            phase=phase,
            kind=kind,
            text=text,
        )

    def _turn_id(self, payload: Dict[str, Any]) -> str:
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or "").strip()
        if turn_id:
            self.current_turn_id = turn_id
            return turn_id
        return self.current_turn_id

    def _commentary_suppressed(self, phase: str) -> bool:
        return phase == "commentary" and self.settings.commentary_voice_mode == "english_status_only"

    def _ensure_english_translator(self) -> Any:
        if self.english_translator is None:
            self.english_translator = EnglishTextTranslator()
        return self.english_translator

    def _translate_to_english(self, text: str) -> str:
        translator = self._ensure_english_translator()
        translated = str(translator.translate(text) or "").strip()
        return " ".join(translated.split()).strip()

    def _normalize_visible_text(self, text: str, phase: str) -> str:
        normalized = text.strip()
        if not normalized:
            return ""
        if self.settings.desktop_text_mode == "translate_to_english":
            if _is_probably_english(normalized):
                return normalized
            try:
                translated = self._translate_to_english(normalized)
            except Exception:
                translated = ""
            return translated or _english_proxy_for_visible(phase, normalized)
        if self.settings.desktop_text_mode == "english_reports_only":
            if _is_probably_english(normalized):
                return normalized
            return _english_proxy_for_visible(phase, normalized)
        if self._commentary_suppressed(phase):
            return ""
        return normalized

    def _normalize_summary_texts(self, summaries: List[str]) -> List[str]:
        cleaned = [summary.strip() for summary in summaries if summary and summary.strip()]
        if not cleaned:
            return []
        if self.settings.desktop_text_mode == "translate_to_english":
            joined = " ".join(cleaned)
            if _is_probably_english(joined):
                return cleaned
            try:
                translated = self._translate_to_english(joined)
            except Exception:
                translated = ""
            return [translated] if translated else [_english_proxy_for_reasoning(cleaned)]
        if self.settings.desktop_text_mode == "english_reports_only":
            joined = " ".join(cleaned)
            if _is_probably_english(joined):
                return cleaned
            return [_english_proxy_for_reasoning(cleaned)]
        return cleaned

    def _new_item_id(self, turn_id: str, phase: str) -> str:
        self._visible_counter += 1
        return f"desktop-message-{phase}-{turn_id}-{self._visible_counter}"

    def _finish_visible_state(self, turn_id: str, phase: str, emit: bool) -> None:
        state = self._visible_states.pop((turn_id, phase), None)
        if state is None:
            return
        normalized = state.text.strip()
        if not normalized:
            return
        visible_key = f"{turn_id}:{phase}:{normalized}"
        if visible_key in self._spoken_visible_keys:
            return
        self._spoken_visible_keys.add(visible_key)
        if not emit:
            return
        self.router.handle_payload(
            {
                "method": "item/completed",
                "params": {
                    "turnId": turn_id,
                    "item": {"id": state.item_id, "type": "agentMessage", "text": normalized},
                },
            }
        )
        self._write_mirror(turn_id=turn_id, phase=phase, kind="assistant_completed", text=normalized, emit=emit)

    def _finish_turn(self, turn_id: str, emit: bool) -> None:
        for current_turn_id, phase in list(self._visible_states.keys()):
            if current_turn_id != turn_id:
                continue
            self._finish_visible_state(turn_id, phase, emit)

    def _turn_has_visible_text(self, turn_id: str, text: str, phase: str) -> bool:
        normalized = self._normalize_visible_text(text, phase)
        if not normalized:
            return False
        for (current_turn_id, _current_phase), state in self._visible_states.items():
            if current_turn_id == turn_id and state.text.strip() == normalized:
                return True
        return False

    def _emit_visible_message(self, turn_id: str, text: str, phase: str, emit: bool) -> None:
        raw_text = text.strip()
        normalized = self._normalize_visible_text(text, phase)
        if not normalized:
            if raw_text and self._commentary_suppressed(phase):
                self._spoken_visible_keys.add(f"{turn_id}:{phase}:{raw_text}")
            return

        visible_key = f"{turn_id}:{phase}:{normalized}"
        if visible_key in self._spoken_visible_keys:
            return

        state_key = (turn_id, phase)
        state = self._visible_states.get(state_key)
        if state is None:
            state = VisibleMessageState(item_id=self._new_item_id(turn_id, phase), phase=phase)
            self._visible_states[state_key] = state
            if emit:
                self.router.handle_payload(
                    {
                        "type": "item_started",
                        "turnId": turn_id,
                        "item": {"id": state.item_id, "type": "agentMessage"},
                    }
                )

        if normalized == state.text:
            return

        if state.text and not normalized.startswith(state.text):
            appended_delta = ""
            if (
                self.settings.desktop_text_mode == "translate_to_english"
                and state.source_text
                and raw_text.startswith(state.source_text)
            ):
                raw_delta = raw_text[len(state.source_text) :].strip()
                if raw_delta:
                    appended_delta = self._normalize_visible_text(raw_delta, phase)
            if appended_delta:
                separator = "" if not state.text or appended_delta.startswith((".", ",", "!", "?", ":", ";")) else " "
                normalized = f"{state.text}{separator}{appended_delta}".strip()
            else:
                self._finish_visible_state(turn_id, phase, emit)
                state = VisibleMessageState(item_id=self._new_item_id(turn_id, phase), phase=phase)
                self._visible_states[state_key] = state
                if emit:
                    self.router.handle_payload(
                        {
                            "type": "item_started",
                            "turnId": turn_id,
                            "item": {"id": state.item_id, "type": "agentMessage"},
                        }
                    )

        delta = normalized[len(state.text) :] if normalized.startswith(state.text) else normalized
        state.source_text = raw_text
        state.text = normalized
        if emit and delta.strip():
            self.router.handle_payload(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"turnId": turn_id, "itemId": state.item_id, "delta": delta},
                }
            )
            self._write_mirror(turn_id=turn_id, phase=phase, kind="assistant_delta", text=delta, emit=emit)

    def _emit_reasoning(self, turn_id: str, summaries: List[str], record_key: str, emit: bool) -> None:
        normalized_summaries = self._normalize_summary_texts(summaries)
        summary_key = f"{turn_id}:{'||'.join(normalized_summaries)}"
        if normalized_summaries and summary_key in self._spoken_summary_keys:
            return
        item_id = f"desktop-reasoning-{record_key}"
        if emit:
            self.router.handle_payload(
                {
                    "type": "item_started",
                    "turnId": turn_id,
                    "item": {"id": item_id, "type": "reasoning"},
                }
            )
            if normalized_summaries:
                self.router.handle_payload(
                    {
                        "type": "item_completed",
                        "turnId": turn_id,
                        "item": {"id": item_id, "type": "reasoning", "summary": normalized_summaries},
                    }
                )
                self._write_mirror(
                    turn_id=turn_id,
                    phase="reasoning",
                    kind="reasoning_summary",
                    text=" ".join(normalized_summaries),
                    emit=emit,
                )
        if normalized_summaries:
            self._spoken_summary_keys.add(summary_key)

    def _emit_tool_activity(self, turn_id: str, record_key: str, emit: bool) -> None:
        tool_key = f"{turn_id}:{record_key}"
        if tool_key in self._tool_started_keys:
            return
        self._tool_started_keys.add(tool_key)
        if not emit:
            return
        self.router.handle_payload(
            {
                "type": "item_started",
                "turnId": turn_id,
                "item": {"id": f"desktop-tool-{record_key}", "type": "toolCommand"},
            }
        )
        self._write_mirror(turn_id=turn_id, phase="tool", kind="status", text="Running tool.", emit=emit)

    def process_record(self, record: Dict[str, Any], *, emit: bool) -> None:
        if not isinstance(record, dict):
            return
        record_type = str(record.get("type") or "")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        timestamp = str(record.get("timestamp") or "")
        record_key = _fingerprint(record_type, timestamp, json.dumps(payload, sort_keys=True, ensure_ascii=False))

        if record_type == "turn_context":
            self._turn_id(payload)
            return

        if record_type == "event_msg":
            event_type = str(payload.get("type") or "")
            previous_turn_id = self.current_turn_id
            turn_id = self._turn_id(payload)
            if event_type == "task_started":
                self._finish_turn(previous_turn_id, emit)
                if emit:
                    self.router.handle_payload({"type": "turn_started", "turnId": turn_id})
                    self._write_mirror(turn_id=turn_id, phase="status", kind="turn_started", text="Working.", emit=emit)
                return
            if event_type == "agent_message":
                phase = str(payload.get("phase") or "assistant")
                text = str(payload.get("message") or "")
                self._emit_visible_message(turn_id, text, phase, emit)
                return
            if event_type == "task_complete":
                last_message = str(payload.get("last_agent_message") or "")
                if last_message and not self._turn_has_visible_text(turn_id, last_message, "final_answer"):
                    self._emit_visible_message(turn_id, last_message, "final_answer", emit)
                self._finish_turn(turn_id, emit)
                if emit:
                    self.router.handle_payload({"type": "turn_completed", "turnId": turn_id})
                    self._write_mirror(
                        turn_id=turn_id,
                        phase="status",
                        kind="turn_completed",
                        text="Turn completed.",
                        emit=emit,
                    )
                return
            return

        if record_type != "response_item":
            return

        response_type = str(payload.get("type") or "")
        turn_id = self.current_turn_id
        if response_type == "reasoning":
            summaries = _extract_summary_texts(payload.get("summary"))
            self._emit_reasoning(turn_id, summaries, record_key, emit)
            return

        if response_type in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or record_key)
            self._emit_tool_activity(turn_id, call_id, emit)
            return

        if response_type != "message":
            return

        if str(payload.get("role") or "") != "assistant":
            return
        text = _extract_message_text(payload.get("content"))
        phase = str(payload.get("phase") or "assistant")
        self._emit_visible_message(turn_id, text, phase, emit)


class DesktopSessionTail:
    def __init__(
        self,
        *,
        thread_record: ThreadRecord,
        controller: VoiceBridgeController,
        poll_interval: float,
        history_bytes: int,
        mirror_writer: DesktopMirrorWriter,
    ) -> None:
        self.thread_record = thread_record
        self.controller = controller
        self.poll_interval = poll_interval
        self.history_bytes = history_bytes
        self.adapter = DesktopSessionBridgeAdapter(
            controller,
            thread_id=thread_record.thread_id,
            cwd=thread_record.cwd,
            mirror_writer=mirror_writer,
        )
        self._handle = None

    def _wait_for_file_update(self) -> None:
        if self._handle is None or not hasattr(select, "kqueue"):
            time.sleep(self.poll_interval)
            return
        kqueue = select.kqueue()
        try:
            event = select.kevent(
                self._handle.fileno(),
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=(
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_ATTRIB
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE
                ),
            )
            kqueue.control([event], 0, 0)
            kqueue.control(None, 1, max(self.poll_interval, 0.2))
        finally:
            kqueue.close()

    def open(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                with self.thread_record.session_file.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    end_offset = handle.tell()
                    start_offset = max(0, end_offset - self.history_bytes)
                    handle.seek(start_offset)
                    if start_offset:
                        handle.readline()
                    for raw_line in handle:
                        try:
                            record = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        self.adapter.process_record(record, emit=False)
                self._handle = self.thread_record.session_file.open("r", encoding="utf-8")
                self._handle.seek(end_offset)
                return
            except FileNotFoundError:
                time.sleep(min(self.poll_interval, 0.1))
        raise SystemExit(130)

    def read_available(self) -> int:
        if self._handle is None:
            return 0
        count = 0
        while True:
            raw_line = self._handle.readline()
            if not raw_line:
                break
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            self.adapter.process_record(record, emit=True)
            count += 1
        return count

    def wait_for_update(self) -> None:
        self._wait_for_file_update()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class ActiveDesktopSessionManager:
    def __init__(
        self,
        *,
        controller: VoiceBridgeController,
        poll_interval: float,
        history_bytes: int,
        fixed_selection: Optional[ThreadRecord] = None,
        resolver: Optional[ActiveDesktopThreadResolver] = None,
        mirror_writer: Optional[DesktopMirrorWriter] = None,
    ) -> None:
        self.controller = controller
        self.poll_interval = poll_interval
        self.history_bytes = history_bytes
        self.fixed_selection = fixed_selection
        self.resolver = resolver
        self.mirror_writer = mirror_writer or DesktopMirrorWriter()
        self.stop_event = threading.Event()
        self._current_selection: Optional[ThreadRecord] = None
        self._current_tail: Optional[DesktopSessionTail] = None

    def _resolve_selection(self) -> Optional[ThreadRecord]:
        if self.fixed_selection is not None:
            return self.fixed_selection
        if self.resolver is None:
            return None
        return self.resolver.resolve()

    def _switch_to(self, selection: ThreadRecord) -> None:
        if self._current_selection is not None and self._current_selection.thread_id != selection.thread_id:
            self.controller.reset_speech_state(cancel_current=True, clear_queue=True, reset_router=True)
        if self._current_tail is not None:
            self._current_tail.close()
        self._current_selection = selection
        self._current_tail = DesktopSessionTail(
            thread_record=selection,
            controller=self.controller,
            poll_interval=self.poll_interval,
            history_bytes=self.history_bytes,
            mirror_writer=self.mirror_writer,
        )
        self._current_tail.open(self.stop_event)

    def stop(self) -> None:
        self.stop_event.set()
        if self._current_tail is not None:
            self._current_tail.close()

    def follow(self) -> None:
        while not self.stop_event.is_set():
            selection = self._resolve_selection()
            if selection is not None:
                if self._current_selection is None or selection.thread_id != self._current_selection.thread_id:
                    self._switch_to(selection)

            if self._current_tail is None:
                time.sleep(self.poll_interval)
                continue

            if self._current_tail.read_available():
                continue

            self._current_tail.wait_for_update()


def resolve_fixed_selection(
    *,
    session_file: Optional[Path],
    thread_id: Optional[str],
    cwd_filter: Optional[str],
    stop_event: threading.Event,
    poll_interval: float,
) -> ThreadRecord:
    resolved_session_file = session_file
    if resolved_session_file is None:
        resolved_session_file = wait_for_session_file(
            thread_id=thread_id,
            cwd_filter=cwd_filter,
            stop_event=stop_event,
            poll_interval=min(poll_interval, 0.05),
        )

    if thread_id:
        record = load_thread_record_by_id(thread_id)
        if record is not None:
            return ThreadRecord(
                thread_id=record.thread_id,
                session_file=resolved_session_file,
                cwd=record.cwd,
                title=record.title,
                updated_at=record.updated_at,
            )

    record = load_thread_record_by_session_file(resolved_session_file)
    if record is not None:
        return record

    return build_thread_record_from_session_file(
        resolved_session_file,
        thread_id=thread_id,
        cwd=cwd_filter,
    )


def build_controller_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        prompt=None,
        backend=args.backend,
        voice=args.voice,
        speed=args.speed,
        cfg=args.cfg,
        steps=args.steps,
        host=args.host,
        port=args.port,
        model=args.model,
        device=DEFAULT_DEVICE,
        panel=args.panel,
        control_port=args.control_port,
        include_reasoning_summary=False,
        speak_code=args.speak_code,
        interrupt_policy=args.interrupt_policy,
        skip_git_repo_check=False,
        auto_start_server=args.auto_start_server,
        codex_args=[],
        speak_assistant_deltas=args.speak_assistant_deltas,
        speak_assistant_completed=args.speak_assistant_completed,
        speak_reasoning_summary=args.speak_reasoning_summary,
        speak_status_announcements=args.speak_status_announcements,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch the active Codex Desktop chat and speak live assistant output.")
    parser.add_argument("--backend", type=str, choices=("auto", "official", "apple"), default=DEFAULT_BACKEND)
    parser.add_argument("--voice", type=str, help="Voice key or speaker name.")
    parser.add_argument("--speed", type=float, help="Speech speed multiplier. Apple backend only.")
    parser.add_argument("--cfg", type=float, default=1.5, help="Classifier-free guidance scale.")
    parser.add_argument("--steps", type=int, help="Optional diffusion steps override.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="Official VibeVoice server host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Official VibeVoice server port.")
    parser.add_argument("--model", type=str, help="Model path or Hugging Face repo ID.")
    parser.add_argument("--panel", action="store_true", help="Launch the macOS menu bar panel or fallback floating control panel.")
    parser.add_argument("--control-port", type=int, help="Optional fixed loopback port for the control API.")
    parser.add_argument("--thread-id", type=str, help="Explicit Codex Desktop thread id. When set, auto-switching is disabled.")
    parser.add_argument("--session-file", type=Path, help="Explicit Codex session rollout JSONL file to follow.")
    parser.add_argument("--cwd-filter", type=str, help="Restrict auto-discovery to a specific workspace cwd.")
    parser.add_argument("--poll-interval", type=float, default=0.15, help="Polling interval while waiting for active-thread changes.")
    parser.add_argument("--history-bytes", type=int, default=512 * 1024, help="How much trailing history to scan for state priming.")
    parser.add_argument("--auto-start-server", dest="auto_start_server", action="store_true", help="Auto-start the official websocket server.")
    parser.add_argument("--no-auto-start-server", dest="auto_start_server", action="store_false", help="Disable auto-start for the official websocket server.")
    parser.set_defaults(auto_start_server=None)
    parser.add_argument("--interrupt-policy", choices=("finish_current", "interrupt_latest", "manual"), help="Speech interruption policy.")
    parser.add_argument("--speak-code", action="store_true", help="Keep simplified inline code instead of stripping code-like content.")

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

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    startup_stop_event = threading.Event()
    fallback_thread_id = os.environ.get("CODEX_THREAD_ID")

    if args.session_file is not None or args.thread_id is not None:
        manager = ActiveDesktopSessionManager(
            controller=VoiceBridgeController(build_controller_args(args)),
            poll_interval=args.poll_interval,
            history_bytes=args.history_bytes,
            fixed_selection=resolve_fixed_selection(
                session_file=args.session_file,
                thread_id=args.thread_id,
                cwd_filter=args.cwd_filter,
                stop_event=startup_stop_event,
                poll_interval=args.poll_interval,
            ),
        )
    else:
        manager = ActiveDesktopSessionManager(
            controller=VoiceBridgeController(build_controller_args(args)),
            poll_interval=args.poll_interval,
            history_bytes=args.history_bytes,
            resolver=ActiveDesktopThreadResolver(
                cwd_filter=args.cwd_filter,
                fallback_thread_id=fallback_thread_id,
            ),
        )

    controller = manager.controller

    def _handle_signal(_signum: int, _frame: Any) -> None:
        startup_stop_event.set()
        manager.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    controller.start()
    try:
        manager.follow()
    finally:
        manager.stop()
        controller.shutdown()


if __name__ == "__main__":
    main()
