"""Structured, de-duplicated activity updates for live Codex turns."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Tuple


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MARKDOWN_BOLD_RE = re.compile(r"\*{2}\s*(.+?)\s*\*{2}", re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")
CONTROL_RE = re.compile(r"[\u200b\u200e\u200f\u202a-\u202e\ufeff]")

ACTIVITY_LEVELS = ("overview", "detail", "trace")
ACTIVE_STATUSES = {"active", "running", "pending", "warning"}
COMPLETE_STATUSES = {"complete", "completed", "done", "success"}


def normalize_activity_text(value: Any, limit: int = 360) -> str:
    """Return safe single-line activity text without raw markdown noise."""
    text = ANSI_ESCAPE_RE.sub("", str(value or ""))
    text = CONTROL_RE.sub("", text).replace("\r", "\n")
    text = text.replace("```", " ").replace("`", "")
    text = re.sub(r"\*{2,}", " ", text)
    text = WHITESPACE_RE.sub(" ", text).strip(" •·:-\t ")
    if limit <= 0:
        return ""
    return text[-limit:]


def activity_fragments(value: Any, limit: int = 240) -> List[str]:
    """Extract concise checkpoints from streamed Markdown-ish reasoning text."""
    raw = ANSI_ESCAPE_RE.sub("", str(value or ""))
    bold_fragments = [
        normalize_activity_text(fragment, limit)
        for fragment in MARKDOWN_BOLD_RE.findall(raw)
    ]
    fragments = [fragment for fragment in bold_fragments if fragment]
    if not fragments and raw.lstrip().startswith("**") and raw.count("**") % 2:
        return []
    if not fragments:
        fragments = [
            normalize_activity_text(fragment, limit)
            for fragment in re.split(r"(?:\*{2,}|\n+)", raw)
        ]
        fragments = [fragment for fragment in fragments if fragment]
    result: List[str] = []
    for fragment in fragments:
        if not result or result[-1].casefold() != fragment.casefold():
            result.append(fragment)
    return result


def merge_activity_delta(previous: Any, incoming: Any, limit: int = 6000) -> str:
    """Merge delta and snapshot-shaped text without replaying duplicate prose."""
    old = str(previous or "")
    new = str(incoming or "")
    if not new:
        return old[-limit:]
    if not old or old == new:
        return new[-limit:]
    if new.startswith(old):
        return new[-limit:]
    if old.startswith(new) or new in old:
        return old[-limit:]
    maximum = min(len(old), len(new))
    overlap = 0
    for size in range(maximum, 0, -1):
        if old[-size:] == new[:size]:
            overlap = size
            break
    return (old + new[overlap:])[-limit:]


def _item_identity(params: Dict[str, Any], fallback: str) -> str:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    for source in (params, item):
        for field in ("itemId", "item_id", "id", "callId", "call_id"):
            value = source.get(field)
            if value is not None and str(value).strip():
                return str(value).strip()
    return fallback


def _item_type(params: Dict[str, Any]) -> str:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    return str(item.get("type") or params.get("itemType") or "").strip()


def _command_text(command: Any, limit: int = 220) -> str:
    if isinstance(command, (list, tuple)):
        value = " ".join(str(part) for part in command)
    else:
        value = str(command or "")
    return normalize_activity_text(value, limit)


def _plan_step_text(step: Dict[str, Any], limit: int = 200) -> str:
    for field in ("description", "title", "text", "step"):
        if step.get(field):
            return normalize_activity_text(step[field], limit)
    return "Untitled step"


@dataclass(frozen=True)
class ActivityEntry:
    """One user-visible activity record, suitable for overview/detail/trace UI."""

    stage: str
    title: str
    detail: str = ""
    level: str = "detail"
    status: str = "active"
    source: str = ""
    key: str = ""
    timestamp: float = 0.0
    count: int = 1

    @property
    def text(self) -> str:
        return f"{self.title}: {self.detail}" if self.detail else self.title

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "title": self.title,
            "detail": self.detail,
            "text": self.text,
            "level": self.level,
            "status": self.status,
            "source": self.source,
            "key": self.key,
            "timestamp": self.timestamp,
            "count": self.count,
        }


@dataclass(frozen=True)
class ActivityStage:
    """A semantic group of records belonging to one work stage."""

    name: str
    status: str
    entries: Tuple[ActivityEntry, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "entries": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class ActivitySnapshot:
    """Stable presentation model consumed by the thinking/activity UI."""

    overview: str
    overview_entry: Optional[ActivityEntry]
    details: Tuple[ActivityEntry, ...]
    trace: Tuple[ActivityEntry, ...]
    stages: Tuple[ActivityStage, ...]
    active_stage: str = ""

    @property
    def entries(self) -> Tuple[ActivityEntry, ...]:
        overview = (self.overview_entry,) if self.overview_entry else ()
        return overview + self.details + self.trace

    def as_dict(self) -> Dict[str, Any]:
        return {
            "overview": self.overview,
            "overview_entry": (
                self.overview_entry.as_dict() if self.overview_entry else None
            ),
            "details": [entry.as_dict() for entry in self.details],
            "trace": [entry.as_dict() for entry in self.trace],
            "stages": [stage.as_dict() for stage in self.stages],
            "active_stage": self.active_stage,
        }


class ActivityFeed:
    """Translate noisy app-server notifications into concise activity snapshots."""

    ITEM_STAGES = {
        "reasoning": ("Reasoning", "Analyzing the request"),
        "commandexecution": ("Command", "Running a command"),
        "filechange": ("Files", "Editing files"),
        "websearch": ("Search", "Searching the web"),
        "mcptoolcall": ("Tool", "Using a connected tool"),
        "collabagenttoolcall": ("Agents", "Coordinating agents"),
        "contextcompaction": ("Context", "Condensing conversation context"),
        "imagegeneration": ("Image", "Generating an image"),
    }

    def __init__(
        self,
        *,
        max_details: int = 16,
        max_trace: int = 36,
        now: Any = time.monotonic,
    ) -> None:
        self.max_details = max(1, int(max_details))
        self.max_trace = max(1, int(max_trace))
        self._now = now
        self._overview: Optional[ActivityEntry] = None
        self._details: List[ActivityEntry] = []
        self._trace: List[ActivityEntry] = []
        self._reasoning_raw = ""
        self._reasoning_fragments: List[str] = []

    @property
    def reasoning_raw(self) -> str:
        return self._reasoning_raw

    def clear(self) -> None:
        self._overview = None
        self._details.clear()
        self._trace.clear()
        self._reasoning_raw = ""
        self._reasoning_fragments.clear()

    def record(
        self,
        *,
        stage: str,
        title: Any,
        detail: Any = "",
        level: str = "detail",
        status: str = "active",
        source: str = "manual",
        key: str = "",
    ) -> ActivitySnapshot:
        normalized_level = level if level in ACTIVITY_LEVELS else "detail"
        normalized_stage = normalize_activity_text(stage, 80) or "Activity"
        normalized_title = normalize_activity_text(title, 260) or normalized_stage
        normalized_detail = normalize_activity_text(detail, 560)
        normalized_status = str(status or "active").strip().lower() or "active"
        normalized_key = key or (
            f"{source}:{normalized_level}:{normalized_stage}:{normalized_title}"
        )
        entry = ActivityEntry(
            stage=normalized_stage,
            title=normalized_title,
            detail=normalized_detail,
            level=normalized_level,
            status=normalized_status,
            source=str(source or "manual"),
            key=normalized_key,
            timestamp=float(self._now()),
        )
        if normalized_level == "overview":
            self._overview = self._merge_entry(self._overview, entry)
        else:
            target = self._details if normalized_level == "detail" else self._trace
            self._upsert(target, entry)
            limit = self.max_details if normalized_level == "detail" else self.max_trace
            if len(target) > limit:
                del target[:-limit]
        return self.snapshot()

    def _merge_entry(
        self, existing: Optional[ActivityEntry], incoming: ActivityEntry
    ) -> ActivityEntry:
        if not existing or existing.key != incoming.key:
            return incoming
        if (
            existing.title == incoming.title
            and existing.detail == incoming.detail
            and existing.status == incoming.status
        ):
            return ActivityEntry(
                **{**existing.__dict__, "timestamp": incoming.timestamp, "count": existing.count + 1}
            )
        if not incoming.detail and existing.detail:
            incoming = replace(incoming, detail=existing.detail)
        return ActivityEntry(
            **{
                **incoming.__dict__,
                "count": existing.count + 1,
            }
        )

    def _upsert(self, target: List[ActivityEntry], incoming: ActivityEntry) -> None:
        for index, existing in enumerate(target):
            if existing.key != incoming.key:
                continue
            target[index] = self._merge_entry(existing, incoming)
            return
        target.append(incoming)

    def _set_overview(
        self,
        stage: str,
        title: Any,
        detail: Any = "",
        *,
        status: str = "active",
        source: str = "",
        key: str = "overview",
    ) -> ActivitySnapshot:
        return self.record(
            stage=stage,
            title=title,
            detail=detail,
            level="overview",
            status=status,
            source=source,
            key=key,
        )

    def snapshot(self) -> ActivitySnapshot:
        grouped: Dict[str, List[ActivityEntry]] = {}
        order: List[str] = []
        for entry in self._details + self._trace:
            if entry.stage not in grouped:
                grouped[entry.stage] = []
                order.append(entry.stage)
            grouped[entry.stage].append(entry)
        stages: List[ActivityStage] = []
        for stage in order:
            entries = tuple(grouped[stage])
            status = next(
                (entry.status for entry in reversed(entries) if entry.status),
                "active",
            )
            stages.append(ActivityStage(stage, status, entries))
        active_stage = ""
        for entry in reversed(self._details + self._trace):
            if entry.status in ACTIVE_STATUSES:
                active_stage = entry.stage
                break
        if not active_stage and self._overview:
            active_stage = self._overview.stage
        return ActivitySnapshot(
            overview=self._overview.text if self._overview else "Getting ready…",
            overview_entry=self._overview,
            details=tuple(self._details),
            trace=tuple(self._trace),
            stages=tuple(stages),
            active_stage=active_stage,
        )

    def ingest(self, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[ActivitySnapshot]:
        """Apply one app-server notification, returning a snapshot if it matters."""
        payload = params if isinstance(params, dict) else {}
        event = str(method or "")
        if event == "item/reasoning/summaryTextDelta":
            return self._ingest_reasoning(payload)
        if event == "turn/plan/updated":
            return self._ingest_plan(payload)
        if event == "turn/diff/updated":
            return self._ingest_diff(payload)
        if event == "item/mcpToolCall/progress":
            return self._ingest_tool_progress(payload)
        if event == "item/started":
            return self._ingest_started(payload)
        if event in (
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        ):
            return self._ingest_output(event, payload)
        if event == "item/completed":
            return self._ingest_completed(payload)
        if event == "bridge/closed":
            return self._set_overview(
                "Connection",
                "Connection lost; reconnecting…",
                status="warning",
                source=event,
            )
        if event == "bridge/reconnected":
            return self._set_overview(
                "Connection",
                "Reconnected; restoring the response…",
                status="active",
                source=event,
            )
        return None

    def _ingest_reasoning(self, params: Dict[str, Any]) -> Optional[ActivitySnapshot]:
        delta = str(params.get("delta") or params.get("text") or "")
        if not delta:
            return None
        self._reasoning_raw = merge_activity_delta(self._reasoning_raw, delta)
        fragments = activity_fragments(self._reasoning_raw)
        if not fragments:
            return None
        new_fragments = [
            fragment
            for fragment in fragments
            if fragment.casefold()
            not in {previous.casefold() for previous in self._reasoning_fragments}
        ]
        for fragment in new_fragments:
            self.record(
                stage="Reasoning",
                title=fragment,
                level="trace",
                status="active",
                source="item/reasoning/summaryTextDelta",
                key=f"reasoning:{fragment.casefold()}",
            )
        self._reasoning_fragments = fragments[-self.max_trace :]
        current = fragments[-1]
        return self._set_overview(
            "Reasoning",
            current,
            detail=(
                f"{len(fragments)} checkpoints"
                if len(fragments) > 1
                else ""
            ),
            source="item/reasoning/summaryTextDelta",
        )

    def _ingest_plan(self, params: Dict[str, Any]) -> ActivitySnapshot:
        raw_plan = params.get("plan")
        plan = raw_plan if isinstance(raw_plan, list) else []
        completed = 0
        active_text = ""
        step_lines: List[str] = []
        for index, raw_step in enumerate(plan, start=1):
            step = raw_step if isinstance(raw_step, dict) else {"description": raw_step}
            status = str(step.get("status") or "pending").strip().lower()
            text = _plan_step_text(step)
            if status in COMPLETE_STATUSES:
                completed += 1
            elif not active_text:
                active_text = text
            step_lines.append(f"{index}. {text} ({status})")
        explanation = normalize_activity_text(params.get("explanation"), 260)
        detail = active_text or explanation or "Plan received"
        self.record(
            stage="Plan",
            title=f"{completed} of {len(plan)} steps complete",
            detail=detail,
            level="detail",
            status="complete" if plan and completed == len(plan) else "active",
            source="turn/plan/updated",
            key="plan",
        )
        if step_lines:
            self.record(
                stage="Plan",
                title="Current plan",
                detail=" • ".join(step_lines),
                level="trace",
                status="active",
                source="turn/plan/updated",
                key="plan:steps",
            )
        return self._set_overview(
            "Plan",
            f"Planning {completed}/{len(plan)}",
            detail=detail,
            status="complete" if plan and completed == len(plan) else "active",
            source="turn/plan/updated",
        )

    def _ingest_diff(self, params: Dict[str, Any]) -> ActivitySnapshot:
        diff = str(params.get("diff") or "")
        additions = sum(
            1
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removals = sum(
            1
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        detail = f"+{additions}  −{removals}"
        self.record(
            stage="Files",
            title="Preparing changes",
            detail=detail,
            level="detail",
            source="turn/diff/updated",
            key="diff",
        )
        return self._set_overview(
            "Files", "Preparing changes", detail, source="turn/diff/updated"
        )

    def _ingest_tool_progress(self, params: Dict[str, Any]) -> ActivitySnapshot:
        message = normalize_activity_text(
            params.get("message") or params.get("progress") or "Running connected tool…",
            360,
        )
        identity = _item_identity(params, "connected-tool")
        self.record(
            stage="Tool",
            title="Connected tool",
            detail=message,
            level="detail",
            source="item/mcpToolCall/progress",
            key=f"tool:{identity}",
        )
        return self._set_overview(
            "Tool",
            "Using a connected tool",
            message,
            source="item/mcpToolCall/progress",
        )

    def _ingest_started(self, params: Dict[str, Any]) -> Optional[ActivitySnapshot]:
        item_type = _item_type(params)
        normalized_type = item_type.replace("_", "").replace("-", "").lower()
        mapping = self.ITEM_STAGES.get(normalized_type)
        if not mapping:
            return None
        stage, action = mapping
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        identity = _item_identity(params, normalized_type or stage.casefold())
        command = _command_text(item.get("command") or params.get("command"))
        detail = command
        self.record(
            stage=stage,
            title=action,
            detail=detail,
            level="detail",
            source="item/started",
            key=f"item:{identity}",
        )
        return self._set_overview(
            stage,
            action,
            detail,
            source="item/started",
        )

    def _ingest_output(self, method: str, params: Dict[str, Any]) -> Optional[ActivitySnapshot]:
        delta = normalize_activity_text(params.get("delta"), 420)
        if not delta:
            return None
        item_type = "Command" if "commandExecution" in method else "Files"
        identity = _item_identity(params, item_type.casefold())
        self.record(
            stage=item_type,
            title="Output",
            detail=delta,
            level="trace",
            source=method,
            key=f"output:{identity}:{delta.casefold()}",
        )
        return self.snapshot()

    def _ingest_completed(self, params: Dict[str, Any]) -> Optional[ActivitySnapshot]:
        item_type = _item_type(params)
        normalized_type = item_type.replace("_", "").replace("-", "").lower()
        mapping = self.ITEM_STAGES.get(normalized_type)
        if not mapping or normalized_type == "reasoning":
            return None
        stage, action = mapping
        identity = _item_identity(params, normalized_type or stage.casefold())
        self.record(
            stage=stage,
            title=action,
            level="detail",
            status="complete",
            source="item/completed",
            key=f"item:{identity}",
        )
        return self._set_overview(
            stage,
            f"Finished: {action}",
            status="complete",
            source="item/completed",
        )


def snapshot_from_events(events: Iterable[Tuple[str, Dict[str, Any]]]) -> ActivitySnapshot:
    """Convenience helper for replay tests and non-GTK consumers."""
    feed = ActivityFeed()
    for method, params in events:
        feed.ingest(method, params)
    return feed.snapshot()
