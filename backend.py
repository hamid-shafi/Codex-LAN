#!/usr/bin/env python3
"""Persistent local broker that keeps ``codex app-server`` alive without the GUI."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import fcntl
from pathlib import Path
from typing import Any, Dict, Optional

from activity_model import merge_activity_delta

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/hamsohbat-{os.getuid()}"))
SOCKET_PATH = RUNTIME_DIR / "hamsohbat-codex.sock"
LOG_PATH = RUNTIME_DIR / "hamsohbat-backend.log"
LOCK_PATH = RUNTIME_DIR / "hamsohbat-backend.lock"
PROTECTED_EVENT_METHODS = {
    "backend/orphanResponse",
    "turn/completed",
    "item/completed",
    "error",
    "thread/deleted",
    "thread/archived",
}
TEXT_DELTA_METHODS = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta",
}
LATEST_ACTIVITY_METHODS = {
    "turn/plan/updated",
    "turn/diff/updated",
    "thread/tokenUsage/updated",
    "item/mcpToolCall/progress",
}


def _activity_event_identity(params: Dict[str, Any]) -> tuple[Any, ...]:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    identity = []
    for key in ("threadId", "turnId", "itemId", "callId"):
        value = params.get(key)
        if value is None:
            value = item.get(key)
        identity.append(value)
    return tuple(identity)


def _same_activity_identity(
    previous: Dict[str, Any], current: Dict[str, Any]
) -> bool:
    previous_identity = _activity_event_identity(previous)
    current_identity = _activity_event_identity(current)
    return any(value is not None for value in current_identity) and (
        previous_identity == current_identity
    )


class Backend:
    def __init__(self) -> None:
        codex = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
        self.process = subprocess.Popen(
            [codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
        self.client: Optional[socket.socket] = None
        self.client_generation = 0
        self.client_ready_generation = 0
        self.client_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.write_lock = threading.RLock()
        self.next_request_id = 0
        self.pending: Dict[int, tuple[int, int, str, Dict[str, Any]]] = {}
        self.initialize_result: Optional[Dict[str, Any]] = None
        self.initializing_id: Optional[int] = None
        self.server_initialized = False
        self.buffered_events: list[Dict[str, Any]] = []
        self.socket_identity: Optional[tuple[int, int]] = None

    def send_process(self, message: Dict[str, Any]) -> None:
        assert self.process.stdin
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self.write_lock:
            try:
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                # The run loop notices child death promptly and performs cleanup.
                return

    def send_client(self, message: Dict[str, Any], generation: Optional[int] = None) -> bool:
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.client_lock:
            if not self.client or (
                generation is not None and generation != self.client_generation
            ):
                return False
            client = self.client
            try:
                client.sendall(payload)
                return True
            except OSError:
                if self.client is client:
                    self.client = None
                return False

    def buffer_event(self, message: Dict[str, Any]) -> None:
        with self.state_lock:
            method = str(message.get("method") or "")
            if method in TEXT_DELTA_METHODS and self.buffered_events:
                previous = self.buffered_events[-1]
                previous_params = previous.get("params") or {}
                params = message.get("params") or {}
                if (
                    previous.get("method") == method
                    and _same_activity_identity(previous_params, params)
                    and isinstance(previous_params.get("delta"), str)
                    and isinstance(params.get("delta"), str)
                ):
                    if method == "item/reasoning/summaryTextDelta":
                        previous_params["delta"] = merge_activity_delta(
                            previous_params["delta"], params["delta"]
                        )
                    else:
                        previous_params["delta"] += params["delta"]
                    return
            if method in LATEST_ACTIVITY_METHODS and self.buffered_events:
                previous = self.buffered_events[-1]
                previous_params = previous.get("params") or {}
                params = message.get("params") or {}
                if (
                    previous.get("method") == method
                    and _same_activity_identity(previous_params, params)
                ):
                    previous["params"] = params
                    return
            self.buffered_events.append(message)
            while len(self.buffered_events) > 1000:
                disposable = next(
                    (
                        index
                        for index, event in enumerate(self.buffered_events)
                        if "id" not in event
                        and event.get("method") not in PROTECTED_EVENT_METHODS
                    ),
                    None,
                )
                if disposable is None:
                    # Approval requests and orphaned RPC results must never be
                    # silently discarded, even after an unusually long outage.
                    break
                self.buffered_events.pop(disposable)

    def send_event(self, message: Dict[str, Any]) -> bool:
        """Never deliver notifications before initialization/replay finishes."""
        with self.client_lock:
            generation = self.client_generation
            if self.client and self.client_ready_generation == generation:
                return self.send_client(message, generation)
            self.buffer_event(message)
            return False

    def finish_client_initialization(
        self, response: Dict[str, Any], generation: int
    ) -> bool:
        with self.client_lock:
            if not self.client or generation != self.client_generation:
                return False
            with self.state_lock:
                events = list(self.buffered_events)
                self.buffered_events.clear()
            if not self.send_client(response, generation):
                return False
            for index, event in enumerate(events):
                if not self.send_client(event, generation):
                    # Put the entire unsent suffix back *before* notifications
                    # that may have arrived while replay was in progress.
                    with self.state_lock:
                        self.buffered_events[0:0] = events[index:]
                    return False
            self.client_ready_generation = generation
            return True

    def read_process(self) -> None:
        assert self.process.stdout
        for raw_line in self.process.stdout:
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if "id" in message and "method" not in message:
                server_id = int(message["id"])
                with self.state_lock:
                    if server_id == self.initializing_id and "result" in message:
                        self.initialize_result = message["result"]
                        self.initializing_id = None
                    mapping = self.pending.pop(server_id, None)
                if mapping:
                    generation, client_id, method, context = mapping
                    message["id"] = client_id
                    if method == "initialize":
                        delivered = self.finish_client_initialization(message, generation)
                    else:
                        delivered = self.send_client(message, generation)
                    if not delivered:
                        orphan = {
                            "method": "backend/orphanResponse",
                            "params": {
                                "requestMethod": method,
                                "context": context,
                                "result": message.get("result"),
                                "error": message.get("error"),
                            },
                        }
                        self.buffer_event(orphan)
                continue
            # Notifications and server-initiated approval requests share the
            # same ordered channel and must both survive GUI reconnects.
            if "method" in message:
                self.send_event(message)

    def log_stderr(self) -> None:
        assert self.process.stderr
        with LOG_PATH.open("a", encoding="utf-8") as log:
            for line in self.process.stderr:
                log.write(line)
                log.flush()

    def handle_client(self, connection: socket.socket) -> None:
        with self.client_lock:
            if self.client:
                try:
                    self.client.close()
                except OSError:
                    pass
            self.client = connection
            self.client_generation += 1
            generation = self.client_generation
            self.client_ready_generation = 0
        try:
            with connection.makefile("r", encoding="utf-8") as reader:
                for raw_line in reader:
                    message = json.loads(raw_line)
                    with self.client_lock:
                        if self.client is not connection or generation != self.client_generation:
                            break
                    method = message.get("method")
                    with self.state_lock:
                        initialized = self.initialize_result
                    if method == "initialize" and "id" in message and initialized is not None:
                        self.finish_client_initialization(
                            {"id": message["id"], "result": initialized}, generation
                        )
                        continue
                    if method == "initialized" and initialized is not None:
                        with self.state_lock:
                            if self.server_initialized:
                                continue
                            self.server_initialized = True
                    if method and "id" in message:
                        client_id = int(message["id"])
                        with self.state_lock:
                            self.next_request_id += 1
                            server_id = self.next_request_id
                            message["id"] = server_id
                            context = message.pop("_hamsohbatContext", {})
                            self.pending[server_id] = (
                                generation, client_id, str(method), context
                            )
                            if method == "initialize":
                                self.initializing_id = server_id
                    self.send_process(message)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            with self.client_lock:
                if self.client is connection:
                    self.client = None
            try:
                connection.close()
            except OSError:
                pass

    def run(self) -> None:
        threading.Thread(target=self.read_process, daemon=True).start()
        threading.Thread(target=self.log_stderr, daemon=True).start()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        stat = SOCKET_PATH.stat()
        self.socket_identity = (stat.st_dev, stat.st_ino)
        server.listen(4)
        server.settimeout(0.5)
        while self.process.poll() is None:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.handle_client, args=(connection,), daemon=True).start()
        server.close()


def main() -> int:
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return 0
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    backend = Backend()

    def stop_backend(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_backend)
    signal.signal(signal.SIGINT, stop_backend)
    try:
        backend.run()
    finally:
        if backend.process.poll() is None:
            backend.process.terminate()
            try:
                backend.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                backend.process.kill()
                backend.process.wait(timeout=3)
        try:
            stat = SOCKET_PATH.stat()
            current_identity = (stat.st_dev, stat.st_ino)
            if backend.socket_identity is not None and current_identity == backend.socket_identity:
                SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
