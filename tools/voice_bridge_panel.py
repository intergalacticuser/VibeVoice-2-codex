from __future__ import annotations

import argparse
import asyncio
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk
import urllib.error
import urllib.request
from typing import Any, Dict

import websockets

from tools.voice_bridge_common import DESKTOP_SPEECH_MODES, INTERRUPT_POLICIES, load_session


def http_json(url: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


class PanelApp:
    def __init__(self, control_url: str) -> None:
        self.control_url = control_url.rstrip("/")
        self.ws_url = self.control_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self.root = tk.Tk()
        self.root.title("Codex Voice Bridge")
        self.root.attributes("-topmost", True)
        self.root.geometry("440x485")
        self.state_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.backend_var = tk.StringVar(value="...")
        self.voice_var = tk.StringVar(value="")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.interrupt_var = tk.StringVar(value="finish_current")
        self.desktop_speech_mode_var = tk.StringVar(value="english_full")
        self.current_var = tk.StringVar(value="No active speech.")
        self.queue_var = tk.StringVar(value="Queue: 0")
        self.status_vars = {
            "speak_assistant_deltas": tk.BooleanVar(value=True),
            "speak_assistant_completed": tk.BooleanVar(value=True),
            "speak_reasoning_summary": tk.BooleanVar(value=True),
            "speak_status_announcements": tk.BooleanVar(value=True),
            "muted": tk.BooleanVar(value=False),
            "read_through": tk.BooleanVar(value=False),
        }
        self.queue_list: tk.Listbox
        self._building = False
        self._build()
        self.root.after(150, self._drain_states)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._state_worker, daemon=True).start()

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        info = ttk.Frame(frame)
        info.pack(fill=tk.X)
        ttk.Label(info, text="Backend").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.backend_var).grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.queue_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        controls = ttk.LabelFrame(frame, text="Speech", padding=8)
        controls.pack(fill=tk.X, pady=(10, 8))
        ttk.Label(controls, text="Voice").grid(row=0, column=0, sticky="w")
        voice_entry = ttk.Entry(controls, textvariable=self.voice_var)
        voice_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(controls, text="Apply", command=self._apply_voice).grid(row=0, column=2, sticky="e")

        ttk.Label(controls, text="Speed").grid(row=1, column=0, sticky="w", pady=(8, 0))
        speed = ttk.Scale(controls, from_=0.7, to=1.5, variable=self.speed_var, orient=tk.HORIZONTAL)
        speed.grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(controls, text="Set", command=self._apply_speed).grid(row=1, column=2, sticky="e", pady=(8, 0))

        ttk.Label(controls, text="Interrupt").grid(row=2, column=0, sticky="w", pady=(8, 0))
        interrupt_menu = ttk.OptionMenu(
            controls,
            self.interrupt_var,
            self.interrupt_var.get(),
            *INTERRUPT_POLICIES,
            command=lambda _value: self._apply_settings(),
        )
        interrupt_menu.grid(row=2, column=1, sticky="w", padx=(8, 6), pady=(8, 0))

        ttk.Label(controls, text="Desktop mode").grid(row=3, column=0, sticky="w", pady=(8, 0))
        speech_mode_menu = ttk.OptionMenu(
            controls,
            self.desktop_speech_mode_var,
            self.desktop_speech_mode_var.get(),
            *DESKTOP_SPEECH_MODES,
            command=lambda _value: self._apply_settings(),
        )
        speech_mode_menu.grid(row=3, column=1, sticky="w", padx=(8, 6), pady=(8, 0))
        controls.columnconfigure(1, weight=1)

        channels = ttk.LabelFrame(frame, text="Channels", padding=8)
        channels.pack(fill=tk.X, pady=(0, 8))
        labels = {
            "speak_assistant_deltas": "Assistant deltas",
            "speak_assistant_completed": "Completed/final",
            "speak_reasoning_summary": "Reasoning summary",
            "speak_status_announcements": "Status",
            "muted": "Mute",
            "read_through": "Read through",
        }
        for index, key in enumerate(labels):
            ttk.Checkbutton(
                channels,
                text=labels[key],
                variable=self.status_vars[key],
                command=self._apply_settings,
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=2)

        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(actions, text="Stop", command=lambda: self._post_action("stop")).pack(side=tk.LEFT)
        ttk.Button(actions, text="Next", command=lambda: self._post_action("next")).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Clear", command=lambda: self._post_action("clear")).pack(side=tk.LEFT)

        current = ttk.LabelFrame(frame, text="Current", padding=8)
        current.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(current, textvariable=self.current_var, wraplength=370, justify=tk.LEFT).pack(fill=tk.X)

        queue_frame = ttk.LabelFrame(frame, text="Queue", padding=8)
        queue_frame.pack(fill=tk.BOTH, expand=True)
        self.queue_list = tk.Listbox(queue_frame, height=8)
        self.queue_list.pack(fill=tk.BOTH, expand=True)

    def _apply_voice(self) -> None:
        self._apply_settings({"voice": self.voice_var.get().strip()})

    def _apply_speed(self) -> None:
        self._apply_settings({"speed": round(float(self.speed_var.get()), 2)})

    def _apply_settings(self, extra: Dict[str, Any] | None = None) -> None:
        payload = {
            "interrupt_policy": self.interrupt_var.get(),
            "desktop_speech_mode": self.desktop_speech_mode_var.get(),
            "speak_assistant_deltas": bool(self.status_vars["speak_assistant_deltas"].get()),
            "speak_assistant_completed": bool(self.status_vars["speak_assistant_completed"].get()),
            "speak_reasoning_summary": bool(self.status_vars["speak_reasoning_summary"].get()),
            "speak_status_announcements": bool(self.status_vars["speak_status_announcements"].get()),
            "muted": bool(self.status_vars["muted"].get()),
            "read_through": bool(self.status_vars["read_through"].get()),
        }
        if extra:
            payload.update(extra)
        try:
            http_json(self.control_url + "/settings", method="POST", payload=payload)
        except urllib.error.URLError:
            pass

    def _post_action(self, action: str) -> None:
        try:
            http_json(self.control_url + f"/actions/{action}", method="POST", payload={})
        except urllib.error.URLError:
            pass

    async def _ws_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url) as websocket:
                    async for message in websocket:
                        self.state_queue.put(json.loads(message))
                        if self.stop_event.is_set():
                            return
            except Exception:
                await asyncio.sleep(1.0)

    def _state_worker(self) -> None:
        asyncio.run(self._ws_loop())

    def _apply_state(self, snapshot: Dict[str, Any]) -> None:
        if not self.root.winfo_exists():
            return
        settings = snapshot.get("settings", {})
        try:
            self.backend_var.set(str(settings.get("backend") or "..."))
            self.queue_var.set(f"Queue: {len(snapshot.get('queue', []))}")
            self.voice_var.set(str(settings.get("voice") or ""))
            self.speed_var.set(float(settings.get("speed") or 1.0))
            self.interrupt_var.set(str(settings.get("interrupt_policy") or "finish_current"))
            self.desktop_speech_mode_var.set(str(settings.get("desktop_speech_mode") or "english_full"))
            for key, variable in self.status_vars.items():
                if key in settings:
                    variable.set(bool(settings[key]))
            current = snapshot.get("current")
            if current:
                self.current_var.set(f"[{current.get('channel')}] {current.get('preview')}")
            else:
                self.current_var.set("No active speech.")
            self.queue_list.delete(0, tk.END)
            for item in snapshot.get("queue", []):
                self.queue_list.insert(tk.END, f"[{item.get('channel')}] {item.get('preview')}")
            geometry = str(settings.get("panel_geometry") or "")
            if geometry and not self._building:
                self._building = True
                self.root.geometry(geometry)
                self._building = False
        except tk.TclError:
            return

    def _drain_states(self) -> None:
        if not self.root.winfo_exists():
            return
        while True:
            try:
                snapshot = self.state_queue.get_nowait()
            except queue.Empty:
                break
            self._apply_state(snapshot)
        if not self.stop_event.is_set() and self.root.winfo_exists():
            self.root.after(150, self._drain_states)

    def _on_close(self) -> None:
        self.stop_event.set()
        geometry = self.root.geometry()
        try:
            http_json(self.control_url + "/settings", method="POST", payload={"panel_geometry": geometry})
        except urllib.error.URLError:
            pass
        self.root.destroy()

    def run(self) -> None:
        try:
            snapshot = http_json(self.control_url + "/state")
            self._apply_state(snapshot)
        except Exception:
            pass
        self.root.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Floating panel client for the Codex voice bridge.")
    parser.add_argument("--control-url", type=str, help="Explicit control API URL. Defaults to the active session file.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    control_url = args.control_url
    if not control_url:
        control_url = str(load_session()["control_url"])
    app = PanelApp(control_url=control_url)
    app.run()


if __name__ == "__main__":
    main()
