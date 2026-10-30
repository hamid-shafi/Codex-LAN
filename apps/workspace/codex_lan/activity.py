"""CODEX: Bounded, public execution activity; never consumes private reasoning."""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict


MAX_ENTRIES = 80
MAX_OUTPUT = 8192
MAX_RUN_OUTPUT = 65536
MAX_STREAMED_OUTPUT = 65536
MAX_DETAIL = 2400
MAX_PROGRESS_ENTRIES = 80
MAX_PROGRESS_MESSAGE = 16384
MAX_PROGRESS_TEXT = 65536
OUTPUT_OMITTED = "\n… [additional command output omitted]\n"
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_SECRET_KEY = r"(?:password|passwd|passphrase|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|authorization|cookie|secret|token|sshpass)"
_ASSIGNMENT = re.compile(r"(?i)([\"']?" + _SECRET_KEY + r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_HEADER = re.compile(r"(?im)((?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_FLAGS = re.compile(r"(?i)((?:--(?:password|token|api-key|secret)|sshpass\s+-p)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")
_JWT_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
_KEY = re.compile(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b")
_URL_AUTH = re.compile(r"((?:https?|socks5h?)://)[^\s/@:]+:[^\s/@]+@", re.I)
_DATA = re.compile(r"data:[^\s,;:]+(?:;base64)?,[^\s\"']+", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)", re.S)
_PATH_TOKEN = re.compile(r"[^\s`\"'<>]+")
_UUID_CHARS = frozenset("0123456789abcdefABCDEF-")


def _word_character(char):
    return char.isalnum() or char == "_"


def _redact_jwt(text):
    """CODEX: Scan disjoint token segments so incomplete JWT prefixes cannot retry long tails."""
    def replace(match):
        token = match.group()
        if "eyJ" not in token or "." not in token:
            return token
        segments = token.split(".")
        if len(segments) < 2:
            return token
        offsets = []
        offset = 0
        for segment in segments:
            offsets.append(offset)
            offset += len(segment) + 1
        pieces = []
        emitted = 0
        index = 0
        while index + 1 < len(segments):
            segment = segments[index]
            candidate = -1
            if len(segment) >= 4 and segments[index + 1]:
                position = 0
                while position <= len(segment) - 4:
                    position = segment.find("eyJ", position)
                    if position < 0 or position > len(segment) - 4:
                        break
                    absolute = match.start() + offsets[index] + position
                    if not absolute or not _word_character(text[absolute - 1]):
                        candidate = offsets[index] + position
                        break
                    position += 3
            if candidate < 0:
                index += 1
                continue
            last = min(index + 2, len(segments) - 1)
            end = offsets[last] + len(segments[last])
            pieces.extend((token[emitted:candidate], "[redacted token]"))
            emitted = end
            index = last + 1
        pieces.append(token[emitted:])
        return "".join(pieces)
    return _JWT_TOKEN.sub(replace, text)


def _redact_server_paths(text):
    """CODEX: Visit each slash once rather than restarting a path regex at every separator."""
    def replace(match):
        token = match.group()
        if "/remote-workers/" not in token:
            return token
        pieces = []
        emitted = 0
        path_start = None
        position = 0
        while True:
            position = token.find("/", position)
            if position < 0:
                break
            absolute = match.start() + position
            preceding = text[absolute - 1] if absolute else ""
            if position and token[position - 1] == "/":
                path_start = position
            elif path_start is None and not (_word_character(preceding) or preceding in {".", "-"}):
                path_start = position
            if path_start is not None and token.startswith("remote-workers/", position + 1):
                identifier_start = position + len("/remote-workers/")
                identifier_end = identifier_start + 36
                identifier = token[identifier_start:identifier_end]
                if len(identifier) == 36 and all(char in _UUID_CHARS for char in identifier):
                    end = len(token) if token[identifier_end:identifier_end + 1] == "/" else identifier_end
                    pieces.extend((token[emitted:path_start], "[server-managed runtime]"))
                    emitted = end
                    path_start = None
                    position = end
                    continue
            position += 1
        pieces.append(token[emitted:])
        return "".join(pieces)
    return _PATH_TOKEN.sub(replace, text)


def redact(value, limit=MAX_DETAIL):
    """CODEX: Strip terminal controls and common credential shapes before persistence."""
    if not isinstance(value, str):
        return ""
    text = _ANSI.sub("", value[:262144]).replace("\r\n", "\n").replace("\r", "\n")
    text = _PRIVATE_KEY.sub("[redacted private key]", text)
    text = _HEADER.sub(r"\1[redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _FLAGS.sub(r"\1[redacted]", text)
    text = _ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _redact_jwt(text)
    text = _KEY.sub("[redacted key]", text)
    text = _URL_AUTH.sub(r"\1[redacted]@", text)
    text = _DATA.sub("[binary data omitted]", text)
    text = _redact_server_paths(text)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    return text if len(text) <= limit else text[:limit] + "\n… [truncated]"


def _plain(value, limit=MAX_DETAIL):
    return redact(value, limit).strip()


def latest_public_line(value):
    """CODEX: Redact multiline context before extracting a short, public progress line."""
    safe = redact(value, 262144)
    for line in reversed(safe.splitlines()):
        line = line.strip()
        if line and line != "… [truncated]":
            return line[:500] + ("…" if len(line) > 500 else "")
    return ""


def _text_output(value, depth=0):
    """CODEX: Extract text content only; never stringify arbitrary tool JSON or blobs."""
    if depth > 8:
        return ""
    if isinstance(value, str):
        if len(value) < 262144 and value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return value
            return _text_output(decoded, depth + 1)
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_output(item, depth + 1) for item in value[:40])))
    if not isinstance(value, dict):
        return ""
    kind = value.get("type", "")
    if kind in {"image", "audio", "resource", "resource_link", "image_url", "inputImage"}:
        return ""
    for key in ("output", "aggregatedOutput", "text", "message"):
        if isinstance(value.get(key), str):
            return _text_output(value[key], depth + 1)
    for key in ("content", "contentItems"):
        if isinstance(value.get(key), list):
            return _text_output(value[key], depth + 1)
    return ""


def _arguments(value):
    if isinstance(value, str) and len(value) < 65536:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
        except ValueError:
            pass
    return value if isinstance(value, dict) else {}


def _tool_detail(item):
    arguments = _arguments(item.get("arguments"))
    command = arguments.get("cmd") or arguments.get("command")
    if isinstance(command, str):
        cwd = arguments.get("workdir") or arguments.get("cwd")
        return _plain(command) + ("\nDirectory: " + _plain(cwd, 400) if cwd else "")
    for key in ("path", "file_path", "query", "q", "url", "pattern"):
        value = arguments.get(key)
        if isinstance(value, str):
            return _plain(value)
    source = arguments.get("code") or arguments.get("source")
    if not source and isinstance(item.get("arguments"), str):
        source = item["arguments"]
    if isinstance(source, str):
        # CODEX: Show nested tool names, not encoded orchestration programs or prompts.
        names = list(dict.fromkeys(re.findall(r"(?:tools|functions)\.([A-Za-z_][\w]*)\s*\(", source[:65536])))[:8]
        if names:
            return "Tools: " + ", ".join(names)
    return ""


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _item_status(item, completed):
    status = str(item.get("status", ""))
    if item.get("error") or item.get("success") is False or status in {"failed", "errored", "error"}:
        return "failed"
    exit_code = _number(item.get("exitCode"))
    if exit_code is not None and exit_code != 0:
        return "failed"
    if status in {"interrupted", "cancelled", "canceled", "declined"}:
        return "cancelled"
    return "completed" if completed or status in {"completed", "succeeded"} else "running"


def normalize_item(item, completed=False):
    """CODEX: Explicit allowlist excludes reasoning and unrelated protocol internals."""
    kind = item.get("type")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    result = {"id": identifier[:200], "status": _item_status(item, completed)}
    if kind == "commandExecution":
        command = _plain(item.get("command"), 1400)
        result.update(kind="command", title=command.split("\n", 1)[0][:160] or "Shell command",
                      detail=command)
        if item.get("cwd"):
            result["detail"] += "\nDirectory: " + _plain(item["cwd"], 500)
        if "aggregatedOutput" in item and item["aggregatedOutput"] is not None:
            result["output"] = _text_output(item["aggregatedOutput"])
    elif kind == "fileChange":
        changes = item.get("changes") or []
        paths = [_plain(change.get("path"), 300) for change in changes[:20] if isinstance(change, dict)]
        result.update(kind="file", title=f"Edit {len(changes)} file" + ("s" if len(changes) != 1 else ""),
                      detail="\n".join(paths))
        if len(paths) == 1:
            result["title"] = "Edit " + paths[0].split("/")[-1]
        if completed:
            diffs = [_plain(change.get("diff"), 4000) for change in changes[:8] if isinstance(change, dict)]
            result["output"] = "\n\n".join(filter(None, diffs))
    elif kind in {"mcpToolCall", "dynamicToolCall"}:
        server = _plain(item.get("server") or item.get("namespace"), 80)
        tool = _plain(item.get("tool"), 100) or "Tool"
        result.update(kind="tool", title=(server + " / " if server else "") + tool,
                      detail=_tool_detail(item))
        if completed:
            result["output"] = _text_output(item.get("error") or item.get("result") or item.get("contentItems"))
    elif kind in {"collabToolCall", "collabAgentToolCall", "subAgentActivity"}:
        action = _plain(item.get("tool") or item.get("kind"), 80) or "Agent task"
        result.update(kind="agent", title="Agent: " + action)
        agents = item.get("agentsStates") or {}
        if isinstance(agents, dict):
            states = [
                _plain(state.get("status"), 80)
                for state in agents.values()
                if isinstance(state, dict)
            ]
            result["detail"] = _plain(", ".join(filter(None, states)), MAX_DETAIL)
        if item.get("agentPath"):
            result["detail"] = _plain(item["agentPath"], 300)
    elif kind == "webSearch":
        query = _plain(item.get("query"), 1200)
        action = item.get("action") or {}
        if isinstance(action, dict) and not query:
            query = _plain(action.get("query") or action.get("url") or action.get("pattern"), 1200)
        result.update(kind="search", title="Web: " + query[:140] if query else "Web search", detail=query)
    elif kind == "contextCompaction":
        result.update(kind="context", title="Compact conversation context")
    else:
        return None
    for source, target in (("exitCode", "exit_code"), ("durationMs", "duration_ms")):
        number = _number(item.get(source))
        if number is not None:
            result[target] = number
    return result


class ActivityLog:
    def __init__(self):
        self.entries = OrderedDict()
        self.count = 0
        self.truncated = False
        self._raw_outputs = {}
        self._emitted_at = {}
        self._seen_events = OrderedDict()
        self._pending_updates = OrderedDict()
        self._streamed_output = {}
        self.progress_entries = OrderedDict()
        self.progress_count = 0
        self.progress_truncated = False
        self._progress_seen = OrderedDict()

    def progress(self, identifier, text, now=None, started_at=None, truncated=False):
        """CODEX: Archive public agent updates independently of the terminal-output budget."""
        if not isinstance(identifier, str) or not identifier or not isinstance(text, str):
            return None
        identifier = identifier[:200]
        now = time.time() if now is None else now
        safe = redact(text[:MAX_PROGRESS_MESSAGE * 2], MAX_PROGRESS_MESSAGE).strip()
        if not safe:
            return None
        old = self.progress_entries.get(identifier)
        truncated = bool(truncated) or len(text) > MAX_PROGRESS_MESSAGE or len(safe) > MAX_PROGRESS_MESSAGE
        safe = safe[:MAX_PROGRESS_MESSAGE]
        if old and old["text"] == safe and bool(old.get("truncated")) == truncated:
            return None
        value = {"id": identifier, "text": safe,
                 "started_at": old["started_at"] if old else started_at or now,
                 "updated_at": now}
        if truncated:
            value["truncated"] = True
            self.progress_truncated = True
        if identifier not in self._progress_seen:
            self.progress_count += 1
        self._progress_seen[identifier] = True
        self._progress_seen.move_to_end(identifier)
        while len(self._progress_seen) > MAX_PROGRESS_ENTRIES * 2:
            self._progress_seen.popitem(last=False)
        self.progress_entries[identifier] = value
        removed = []
        total = sum(len(entry["text"]) for entry in self.progress_entries.values())
        while len(self.progress_entries) > MAX_PROGRESS_ENTRIES or total > MAX_PROGRESS_TEXT:
            key, discarded = self.progress_entries.popitem(last=False)
            total -= len(discarded["text"])
            removed.append(key)
            self.progress_truncated = True
        return {"progress_entry": dict(value), "progress_removed_ids": removed,
                "progress_log_count": self.progress_count,
                "progress_log_truncated": self.progress_truncated}

    def remove_progress(self, identifier):
        """CODEX: A late explicit final phase must not leave a duplicate final answer in logs."""
        identifier = identifier[:200] if isinstance(identifier, str) else ""
        if self.progress_entries.pop(identifier, None) is None:
            return None
        self._progress_seen.pop(identifier, None)
        self.progress_count = max(0, self.progress_count - 1)
        return {"progress_removed_ids": [identifier],
                "progress_log_count": self.progress_count,
                "progress_log_truncated": self.progress_truncated}

    def restore_progress(self, entries, count=None, truncated=False):
        """CODEX: Validate and re-bound durable public messages before exposing a recovered run."""
        if not isinstance(entries, list):
            return
        for entry in entries[-MAX_PROGRESS_ENTRIES:]:
            if not isinstance(entry, dict):
                continue
            self.progress(entry.get("id"), entry.get("text"),
                          now=_number(entry.get("updated_at")) or time.time(),
                          started_at=_number(entry.get("started_at")))
            if entry.get("truncated"):
                restored = self.progress_entries.get(str(entry.get("id", ""))[:200])
                if restored is not None:
                    restored["truncated"] = True
                    self.progress_truncated = True
        if isinstance(count, int) and not isinstance(count, bool):
            self.progress_count = max(self.progress_count, count)
        self.progress_truncated |= bool(truncated) or len(entries) > MAX_PROGRESS_ENTRIES

    def progress_snapshot(self):
        return {"progress_log": [dict(entry) for entry in self.progress_entries.values()],
                "progress_log_count": self.progress_count,
                "progress_log_truncated": self.progress_truncated}

    def _bounded_output(self, identifier, raw):
        safe = redact(raw, 262144)
        available = max(0, MAX_RUN_OUTPUT - sum(len(entry.get("output", ""))
                        for key, entry in self.entries.items() if key != identifier))
        needed = min(MAX_OUTPUT, len(safe))
        if available < needed:
            # CODEX: Preserve recent terminal output by shortening older output, not new results.
            for key, entry in self.entries.items():
                if key == identifier or not entry.get("output"):
                    continue
                before = len(entry["output"])
                keep = min(256, max(0, before - (needed - available)))
                entry["output"] = entry["output"][-keep:] if keep else ""
                entry["output_truncated"] = True
                entry["updated_at"] = time.time()
                self._pending_updates[key] = dict(entry)
                available += before - keep
                if available >= needed:
                    break
        limit = min(MAX_OUTPUT, available)
        return (safe[-limit:] if limit else ""), len(safe) > limit

    def take_updates(self):
        updates = list(self._pending_updates.values())
        self._pending_updates.clear()
        return updates

    def _save(self, incoming, now=None):
        now = time.time() if now is None else now
        identifier = incoming["id"]
        old = self.entries.get(identifier)
        value = dict(old or {})
        value.update(incoming)
        if old and old["status"] != "running" and incoming["status"] == "running":
            return None
        if "output" in incoming:
            value["output"], value["output_truncated"] = self._bounded_output(identifier, incoming["output"])
        value.setdefault("started_at", now)
        for key in ("detail", "title"):
            if key in value:
                value[key] = _plain(value[key], MAX_DETAIL if key == "detail" else 180)
        if old and all(value.get(key) == old.get(key) for key in set(value) | set(old) if key != "updated_at"):
            return None
        value["updated_at"] = now
        if old is None:
            self.count += 1
        self.entries[identifier] = value
        while len(self.entries) > MAX_ENTRIES:
            remove = next((key for key, entry in self.entries.items() if entry["status"] != "running"), next(iter(self.entries)))
            self.entries.pop(remove)
            self._raw_outputs.pop(remove, None)
            self._emitted_at.pop(remove, None)
            self._pending_updates.pop(remove, None)
            self.truncated = True
        return dict(value)

    def item(self, item, completed=False, now=None):
        normalized = normalize_item(item, completed)
        if normalized is None:
            return None
        identifier = normalized["id"]
        if completed and "output" not in normalized and identifier in self._raw_outputs:
            normalized["output"] = self._raw_outputs[identifier]
        result = self._save(normalized, now)
        if completed:
            self._raw_outputs.pop(identifier, None)
            self._emitted_at.pop(identifier, None)
            self._streamed_output.pop(identifier, None)
        return result

    def output(self, params, now=None):
        identifier = params.get("itemId")
        old = self.entries.get(identifier)
        if old is None or old.get("status") != "running":
            return None
        now = time.time() if now is None else now
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        seen = self._streamed_output.get(identifier, 0)
        if seen >= MAX_STREAMED_OUTPUT:
            return None
        event_id = params.get("eventId") or params.get("sequenceNumber")
        if event_id is not None:
            marker = (identifier, str(event_id))
            if marker in self._seen_events:
                return None
            self._seen_events[marker] = True
            while len(self._seen_events) > 256:
                self._seen_events.popitem(last=False)
        remaining = MAX_STREAMED_OUTPUT - seen
        accepted = delta[:remaining]
        self._streamed_output[identifier] = seen + len(accepted)
        if len(delta) > remaining:
            accepted += OUTPUT_OMITTED
        # CODEX: Keep a bounded raw tail so split credentials can be redacted together.
        available = max(MAX_OUTPUT, 131072 - sum(len(value) for key, value in self._raw_outputs.items() if key != identifier))
        raw = (self._raw_outputs.get(identifier, old.get("output", "")) + accepted)[-min(16384, available):]
        self._raw_outputs[identifier] = raw
        last_time = self._emitted_at.get(identifier, 0)
        if now - last_time < 0.3:
            return None
        self._emitted_at[identifier] = now
        return self._save({"id": identifier, "status": "running", "output": raw}, now)

    def finish(self, status):
        changes = []
        for entry in list(self.entries.values()):
            if entry["status"] == "running":
                incoming = {"id": entry["id"], "status": "cancelled",
                            "detail": (entry.get("detail", "") + "\nStep completion was not reported.").strip()}
                if entry["id"] in self._raw_outputs:
                    incoming["output"] = self._raw_outputs[entry["id"]]
                result = self._save(incoming)
                if result:
                    changes.append(result)
        self._raw_outputs.clear()
        self._emitted_at.clear()
        self._streamed_output.clear()
        return changes

    def flush(self, now=None):
        """CODEX: Publish coalesced trailing output even when a command stops producing new chunks."""
        now = time.time() if now is None else now
        changes = []
        for identifier, raw in list(self._raw_outputs.items()):
            entry = self.entries.get(identifier)
            if not entry or entry.get("status") != "running" or now - self._emitted_at.get(identifier, 0) < 0.3:
                continue
            update = self._save({"id": identifier, "status": "running", "output": raw}, now)
            if update:
                self._emitted_at[identifier] = now
                changes.append(update)
        return changes

    def restore(self, entries, count=None, truncated=False):
        for entry in entries[-MAX_ENTRIES:]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            self._save(entry, entry.get("updated_at") or time.time())
        self.count = max(self.count, count or 0)
        self.truncated = truncated or len(entries) > MAX_ENTRIES

    def snapshot(self, status="running"):
        entries = [dict(entry) for entry in self.entries.values()]
        running = [entry for entry in entries if entry["status"] == "running"]
        if status not in {"running", "queued"}:
            summary = {"completed": "Work completed", "failed": "Run failed", "cancelled": "Stopped", "interrupted": "Connection interrupted"}.get(status, "Work finished")
        elif running:
            summary = running[-1]["title"]
            if len(running) > 1:
                summary += f" · {len(running)} active steps"
        else:
            summary = "Waiting for model" if entries else "Starting on your computer"
        return {"activities": entries, "activity": summary,
                "activity_count": self.count, "activity_truncated": self.truncated,
                **self.progress_snapshot()}
