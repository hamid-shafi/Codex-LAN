# -*- coding: utf-8 -*-
"""Small JSON-RPC client for ``codex app-server``.

The bridge deliberately keeps GTK out of the protocol layer.  Callers provide a
``dispatch`` function (GLib.idle_add in the desktop app) so callbacks are always
delivered on the UI thread.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union


Callback = Callable[[Dict[str, Any]], None]
ErrorCallback = Callable[[Dict[str, Any]], None]
Dispatcher = Callable[..., Any]
TRANSPORT_DISCONNECTED = "transport_disconnected"
ImagePaths = Optional[Union[str, os.PathLike[str], Iterable[Union[str, os.PathLike[str]]]]]


class CodexBridge:
    """Manage one long-lived Codex app-server process over stdio."""

    def __init__(
        self,
        dispatch: Dispatcher,
        on_event: Callable[[str, Dict[str, Any]], None],
        on_server_request: Optional[
            Callable[[int, str, Dict[str, Any]], None]
        ] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.dispatch = dispatch
        self.on_event = on_event
        self.on_server_request = on_server_request
        self.on_log = on_log or (lambda _line: None)
        self.process: Optional[subprocess.Popen[str]] = None
        self.socket: Optional[socket.socket] = None
        self.reader: Any = None
        self._pending: Dict[int, Tuple[Optional[Callback], Optional[ErrorCallback]]] = {}
        self._request_timers: Dict[int, threading.Timer] = {}
        self._request_id = 0
        self._lock = threading.RLock()
        self._closed = False
        self._disconnect_reported = False

    @staticmethod
    def find_codex() -> Optional[str]:
        candidate = shutil.which("codex")
        if candidate:
            return candidate
        fallback = Path.home() / ".local" / "bin" / "codex"
        return str(fallback) if fallback.exists() else None

    def start(self, on_ready: Callback, on_error: ErrorCallback) -> None:
        """Connect without blocking GTK's main loop during backend startup."""
        threading.Thread(
            target=self._connect_and_initialize,
            args=(on_ready, on_error),
            daemon=True,
            name="codex-connect",
        ).start()

    def _connect_and_initialize(
        self, on_ready: Callback, on_error: ErrorCallback
    ) -> None:
        if self._closed:
            return
        codex = self.find_codex()
        if not codex:
            self.dispatch(on_error, {"message": "Codex CLI was not found on this system."})
            return

        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/hamsohbat-{os.getuid()}"))
        socket_path = runtime_dir / "hamsohbat-codex.sock"
        backend_path = Path(__file__).resolve().parent / "backend.py"
        try:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                candidate.connect(str(socket_path))
                self.socket = candidate
            except OSError:
                candidate.close()
                subprocess.Popen(
                    [sys.executable, str(backend_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                for _attempt in range(50):
                    if self._closed:
                        return
                    time.sleep(0.1)
                    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        candidate.connect(str(socket_path))
                        self.socket = candidate
                        break
                    except OSError:
                        candidate.close()
                else:
                    raise RuntimeError("Persistent Codex backend did not start")
            self.reader = self.socket.makefile("r", encoding="utf-8", errors="replace")
        except (OSError, RuntimeError) as exc:
            self.dispatch(on_error, {"message": str(exc)})
            return

        if self._closed:
            try:
                self.socket.close()
            except (AttributeError, OSError):
                pass
            return

        threading.Thread(target=self._read_stdout, daemon=True).start()

        def initialized(_result: Dict[str, Any]) -> None:
            self.notify("initialized", {})
            on_ready(_result)

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_desktop",
                    "title": "Codex",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                },
            },
            initialized,
            on_error,
        )

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        callback: Optional[Callback] = None,
        errback: Optional[ErrorCallback] = None,
        context: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 45.0,
    ) -> int:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._pending[request_id] = (callback, errback)
            timer = threading.Timer(
                timeout_seconds, self._expire_request, args=(request_id, method)
            )
            timer.daemon = True
            self._request_timers[request_id] = timer
            message = {"method": method, "id": request_id, "params": params or {}}
            if context:
                message["_hamsohbatContext"] = context
            if not self._write(message):
                self._pending.pop(request_id, None)
                self._request_timers.pop(request_id, None)
                timer.cancel()
            else:
                timer.start()
            return request_id

    def _expire_request(self, request_id: int, method: str) -> None:
        with self._lock:
            callbacks = self._pending.pop(request_id, None)
            self._request_timers.pop(request_id, None)
        if callbacks:
            _callback, errback = callbacks
            if errback:
                self.dispatch(
                    errback,
                    {"message": f"The {method} request timed out."},
                )

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._write({"method": method, "params": params or {}})

    def respond(self, request_id: int, result: Dict[str, Any]) -> None:
        self._write({"id": request_id, "result": result})

    def respond_error(self, request_id: int, message: str) -> None:
        self._write({"id": request_id, "error": {"code": -32000, "message": message}})

    def list_models(self, callback: Callback, errback: ErrorCallback) -> int:
        return self.request(
            "model/list",
            {"limit": 50, "includeHidden": False},
            callback,
            errback,
        )

    @staticmethod
    def _normalize_image_paths(*sources: ImagePaths) -> List[str]:
        """Return ordered, de-duplicated local image paths."""
        paths: List[str] = []
        for source in sources:
            if source is None:
                continue
            if isinstance(source, (str, os.PathLike)):
                values = [source]
            else:
                values = source
            for value in values:
                path = os.fspath(value)
                if path and path not in paths:
                    paths.append(path)
        return paths

    def start_thread(
        self,
        model: str,
        cwd: str,
        callback: Callback,
        errback: ErrorCallback,
        room_id: Optional[int] = None,
        branch_id: Optional[int] = None,
    ) -> int:
        context = None
        if room_id is not None:
            context = {"roomId": room_id, "operation": "thread/start"}
        elif branch_id is not None:
            context = {"branchId": branch_id, "operation": "thread/start"}
        return self.request(
            "thread/start",
            {
                "model": model,
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "personality": "friendly",
                "serviceName": "codex_desktop",
            },
            callback,
            errback,
            context=context,
        )

    def resume_thread(
        self,
        thread_id: str,
        model: str,
        cwd: str,
        callback: Callback,
        errback: ErrorCallback,
        room_id: Optional[int] = None,
        branch_id: Optional[int] = None,
    ) -> int:
        context = None
        if room_id is not None:
            context = {"roomId": room_id, "operation": "thread/resume"}
        elif branch_id is not None:
            context = {"branchId": branch_id, "operation": "thread/resume"}
        return self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "model": model,
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "personality": "friendly",
            },
            callback,
            errback,
            context=context,
        )

    def start_turn(
        self,
        thread_id: str,
        text: str,
        model: str,
        effort: Optional[str],
        cwd: str,
        image_path: ImagePaths,
        callback: Callback,
        errback: ErrorCallback,
        room_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        image_paths: ImagePaths = None,
    ) -> int:
        inputs = []
        if text.strip():
            inputs.append({"type": "text", "text": text})
        for path in self._normalize_image_paths(image_path, image_paths):
            inputs.append({"type": "localImage", "path": path})

        params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
            "cwd": cwd,
            "model": model,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "personality": "friendly",
            "summary": "concise",
        }
        if effort:
            params["effort"] = effort
        context = None
        if room_id is not None:
            context = {"roomId": room_id, "operation": "turn/start"}
        elif branch_id is not None:
            context = {"branchId": branch_id, "operation": "turn/start"}
        return self.request("turn/start", params, callback, errback, context=context)

    def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        image_path: ImagePaths,
        callback: Callback,
        errback: ErrorCallback,
        room_id: Optional[int] = None,
        image_paths: ImagePaths = None,
    ) -> int:
        inputs = []
        if text.strip():
            inputs.append({"type": "text", "text": text})
        for path in self._normalize_image_paths(image_path, image_paths):
            inputs.append({"type": "localImage", "path": path})
        return self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": inputs,
            },
            callback,
            errback,
            context={"roomId": room_id, "operation": "turn/steer"}
            if room_id
            else None,
        )

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def set_thread_name(
        self,
        thread_id: str,
        name: str,
        callback: Optional[Callback] = None,
        errback: Optional[ErrorCallback] = None,
    ) -> int:
        return self.request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
            callback,
            errback,
        )

    def delete_thread(
        self,
        thread_id: str,
        callback: Callback,
        errback: ErrorCallback,
    ) -> int:
        return self.request(
            "thread/delete", {"threadId": thread_id}, callback, errback
        )

    def close(self) -> None:
        self._closed = True
        with self._lock:
            timers = list(self._request_timers.values())
            self._request_timers.clear()
            self._pending.clear()
        for timer in timers:
            timer.cancel()
        try:
            if self.socket:
                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()
        except OSError:
            pass

    def _write(self, message: Dict[str, Any]) -> bool:
        if not self.socket:
            self._mark_disconnected()
            return False
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                self.socket.sendall((payload + "\n").encode("utf-8"))
            return True
        except (BrokenPipeError, ConnectionError, OSError):
            self._mark_disconnected()
            return False

    def _mark_disconnected(self) -> None:
        with self._lock:
            if self._closed or self._disconnect_reported:
                return
            self._disconnect_reported = True
            pending = list(self._pending.values())
            self._pending.clear()
            timers = list(self._request_timers.values())
            self._request_timers.clear()
        for timer in timers:
            timer.cancel()
        # A socket loss is not an app-server operation failure.  The persistent
        # broker keeps in-flight requests and will replay their responses after
        # the GUI reconnects.  Consumers use this marker to keep live turn UI
        # intact instead of persisting a bogus assistant error message.
        error = {
            "code": TRANSPORT_DISCONNECTED,
            "message": "The connection to Codex was lost.",
            "transient": True,
        }
        for _callback, errback in pending:
            if errback:
                self.dispatch(errback, error)
        self.dispatch(self.on_event, "bridge/closed", error)

    def _read_stdout(self) -> None:
        if not self.reader:
            return
        for raw_line in self.reader:
            if self._closed:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.dispatch(self.on_log, line)
                continue
            self._route(message)
        if not self._closed:
            self._mark_disconnected()

    def _read_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        for raw_line in process.stderr:
            if self._closed:
                break
            line = raw_line.strip()
            if line:
                self.dispatch(self.on_log, line)

    def _route(self, message: Dict[str, Any]) -> None:
        if "method" in message:
            method = str(message.get("method"))
            params = message.get("params") or {}
            if "id" in message:
                request_id = int(message["id"])
                if self.on_server_request:
                    self.dispatch(self.on_server_request, request_id, method, params)
                else:
                    self.respond_error(request_id, "Client does not handle server requests")
            else:
                self.dispatch(self.on_event, method, params)
            return

        request_id = message.get("id")
        if request_id is None:
            return
        with self._lock:
            callbacks = self._pending.pop(int(request_id), (None, None))
            timer = self._request_timers.pop(int(request_id), None)
        if timer:
            timer.cancel()
        callback, errback = callbacks
        if "error" in message:
            if errback:
                self.dispatch(errback, message.get("error") or {})
        elif callback:
            self.dispatch(callback, message.get("result") or {})
