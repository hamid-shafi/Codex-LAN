#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex — a GTK desktop client for Codex app-server."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from branch_controller import BranchController  # noqa: E402
from branch_view import BranchPanel  # noqa: E402
from activity_model import ActivityFeed, ActivitySnapshot
from attachment_preview import (
    AttachmentImageError,
    inspect_attachment_image,
    load_attachment_image_async,
    show_attachment_preview,
)
from codex_bridge import CodexBridge  # noqa: E402
from markdown_view import MarkdownView, content_prefers_rtl  # noqa: E402
from storage import Store  # noqa: E402


APP_ID = "dev.codex.Desktop"
APP_NAME = "Codex"
APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "assets" / "codex.svg"
DEFAULT_CWD = str(Path.home())
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COMPOSER_RTL_MARK = "\u200f"
COMPOSER_HINT = "Press Enter twice to send • Shift+Enter for a new line • Ctrl+V to paste an image"
SEND_ENTER_KEYS = (
    Gdk.KEY_Return,
    Gdk.KEY_KP_Enter,
    Gdk.KEY_ISO_Enter,
    Gdk.KEY_3270_Enter,
)
ROOM_INITIAL_RENDER_COUNT = 10
ROOM_OLDER_PAGE_SIZE = 24
COMPOSER_DIRECTION_IDLE_BATCH_SIZE = 24
FINAL_ANSWER_GRACE_MILLISECONDS = 1500
DISCONNECTED_TURN_TIMEOUT_SECONDS = 90.0

# Keep the app-server's wire values intact while presenting stable, readable
# English labels.  The protocol currently returns these in the same order, but
# sorting locally protects the UI from server/cache ordering changes.
EFFORT_ORDER = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
EFFORT_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra",
    "max": "Max",
    "ultra": "Ultra",
}
EFFORT_DESCRIPTIONS = {
    "none": "No additional reasoning; lowest latency.",
    "minimal": "Minimal reasoning for simple, latency-sensitive work.",
    "low": "Fast responses with lighter reasoning.",
    "medium": "Balances speed and reasoning depth for everyday tasks.",
    "high": "Greater reasoning depth for complex problems.",
    "xhigh": "Extra high reasoning depth for complex problems.",
    "max": "Maximum reasoning depth for the hardest problems.",
    "ultra": "Maximum reasoning with automatic task delegation.",
}
GPT_6_ASTRA_ID = "gpt-6-astra"


def is_gpt_6_astra(model_id: Optional[str], model: Dict[str, Any]) -> bool:
    """Whether dynamic app-server metadata describes GPT-6 Astra."""
    identity = model_id or model.get("id") or model.get("model") or ""
    return str(identity).strip().lower() == GPT_6_ASTRA_ID


def room_is_busy(
    room: Optional[Dict[str, Any]], room_turns: Dict[int, Dict[str, Any]]
) -> bool:
    """Return whether *the supplied room* owns an in-flight turn.

    Header selectors represent the active room only.  Looking at the number of
    global turns would incorrectly lock an idle room whenever another room is
    streaming in the background.
    """
    if not room or room.get("id") is None:
        return False
    try:
        return int(room["id"]) in room_turns
    except (TypeError, ValueError):
        return False


def ordered_effort_options(
    model: Dict[str, Any], model_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Normalize app-server effort metadata for the GTK selector."""
    default = str(model.get("defaultReasoningEffort") or "").strip().lower()
    rank = {value: index for index, value in enumerate(EFFORT_ORDER)}
    options: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for source_index, entry in enumerate(
        model.get("supportedReasoningEfforts") or []
    ):
        effort = str(entry.get("reasoningEffort") or "").strip().lower()
        if not effort or effort in seen:
            continue
        seen.add(effort)
        base_label = EFFORT_LABELS.get(
            effort, effort.replace("_", " ").replace("-", " ").title()
        )
        is_default = effort == default
        options.append(
            {
                "id": effort,
                "label": f"{base_label} ★" if is_default else base_label,
                "base_label": base_label,
                "description": str(entry.get("description") or "").strip()
                or EFFORT_DESCRIPTIONS.get(effort, "Reasoning effort for this model."),
                "is_default": is_default,
                "source_index": source_index,
            }
        )
    options.sort(
        key=lambda item: (
            rank.get(str(item["id"]), len(rank)),
            item["source_index"],
        )
    )
    return options


def preferred_model_id(
    models: Dict[str, Dict[str, Any]], requested: Optional[str]
) -> Optional[str]:
    """Select a room's saved model, then the protocol default, then the first."""
    if requested and requested in models:
        return requested
    return next(
        (model_id for model_id, data in models.items() if data.get("isDefault")),
        next(iter(models), None),
    )


def model_display_label(model_id: str, model: Dict[str, Any]) -> str:
    label = str(model.get("displayName") or model_id).strip() or model_id
    if is_gpt_6_astra(model_id, model) and label.lower() == GPT_6_ASTRA_ID:
        label = "GPT-6 Astra"
    # The full name and ID remain in the tooltip; remove a redundant product
    # suffix here so the header can shrink cleanly on compact windows.
    label = re.sub(r"\s+Codex$", "", label, flags=re.IGNORECASE)
    return f"{label} ★" if model.get("isDefault") else label


def model_tooltip_text(model_id: str, model: Dict[str, Any]) -> str:
    label = str(model.get("displayName") or model_id).strip() or model_id
    if is_gpt_6_astra(model_id, model) and label.lower() == GPT_6_ASTRA_ID:
        label = "GPT-6 Astra"
    description = str(model.get("description") or "").strip()
    lines = [label]
    if description:
        lines.append(description)
    if model.get("isDefault"):
        lines.append("Recommended default model")
    if is_gpt_6_astra(model_id, model):
        efforts = ordered_effort_options(model, model_id=model_id)
        if efforts:
            lines.append(
                "Reasoning: "
                + ", ".join(str(option["base_label"]) for option in efforts)
            )
    lines.append(f"Model ID: {model_id}")
    upgrade = model.get("upgrade")
    if upgrade:
        lines.append(f"Upgrade available: {upgrade}")
    return "\n".join(lines)


def clean_activity_text(value: str, limit: int = 500) -> str:
    return " ".join(ANSI_ESCAPE_RE.sub("", value).split())[-limit:]


def activity_feed_for_state(state: Dict[str, Any]) -> ActivityFeed:
    """Lazily attach one structured activity feed to an in-flight turn state."""
    feed = state.get("activity_feed")
    if not isinstance(feed, ActivityFeed):
        feed = ActivityFeed()
        state["activity_feed"] = feed
    return feed


def set_state_activity_presentation(
    state: Dict[str, Any], presentation: str
) -> None:
    normalized = str(presentation or "working").strip().lower()
    if normalized not in {
        "working",
        "reconnecting",
        "restoring",
        "stopping",
        "complete",
    }:
        normalized = "working"
    state["activity_presentation"] = normalized
    bubble = state.get("bubble")
    setter = getattr(bubble, "set_activity_presentation", None)
    if callable(setter):
        setter(normalized)


def present_activity_snapshot(
    state: Dict[str, Any], snapshot: Optional[ActivitySnapshot]
) -> Optional[ActivitySnapshot]:
    """Keep legacy bubbles working while preferring the structured activity API."""
    if snapshot is None:
        return None
    state["activity_snapshot"] = snapshot
    state["activity_text"] = snapshot.overview
    bubble = state.get("bubble")
    if bubble:
        structured = getattr(bubble, "set_activity_snapshot", None)
        if callable(structured):
            structured(snapshot)
        else:
            bubble.set_activity(snapshot.overview)
    set_state_activity_presentation(
        state, str(state.get("activity_presentation") or "working")
    )
    return snapshot


def ingest_state_activity(
    state: Dict[str, Any], method: str, params: Dict[str, Any]
) -> Optional[ActivitySnapshot]:
    """Normalize one protocol notification and present only its useful summary."""
    if method not in {"bridge/closed", "bridge/reconnected"} and str(
        state.get("activity_presentation") or ""
    ) in {"reconnecting", "restoring"}:
        set_state_activity_presentation(state, "working")
    feed = activity_feed_for_state(state)
    snapshot = feed.ingest(method, params)
    if method == "item/reasoning/summaryTextDelta":
        state["reasoning_summary"] = snapshot.overview if snapshot else ""
    return present_activity_snapshot(state, snapshot)


def _agent_message_item_id(params: Dict[str, Any], item: Dict[str, Any]) -> str:
    item_id = params.get("itemId") or params.get("item_id")
    if item_id is None:
        item_id = item.get("id") or item.get("itemId") or item.get("item_id")
    return str(item_id) if item_id is not None else ""


def _streaming_items(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = state.get("streaming_items")
    if not isinstance(items, list):
        items = []
        state["streaming_items"] = items
    if not items and state.get("streaming_text"):
        items.append(
            {
                "id": str(state.get("streaming_item_id") or ""),
                "text": str(state.get("streaming_text") or ""),
                "completed": False,
            }
        )
    return items


def _new_anonymous_streaming_item_id(state: Dict[str, Any]) -> str:
    try:
        counter = int(state.get("streaming_anonymous_item_counter") or 0) + 1
    except (TypeError, ValueError):
        counter = 1
    state["streaming_anonymous_item_counter"] = counter
    return f"\x00anonymous-{counter}"


def _find_streaming_item(
    items: List[Dict[str, Any]], item_id: str
) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    return next(
        (entry for entry in items if str(entry.get("id") or "") == item_id),
        None,
    )


def _last_open_streaming_item(
    items: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    return next(
        (entry for entry in reversed(items) if not bool(entry.get("completed"))),
        None,
    )


def _rebuild_streaming_text(state: Dict[str, Any]) -> str:
    text = ""
    last_item_id = ""
    for entry in _streaming_items(state):
        item_text = str(entry.get("text") or "")
        if not item_text:
            continue
        if text:
            text = text.rstrip() + "\n\n"
        text += item_text
        last_item_id = str(entry.get("id") or "")
    state["streaming_text"] = text
    state["streaming_item_id"] = last_item_id
    return text


def append_agent_message_delta(state: Dict[str, Any], params: Dict[str, Any]) -> str:
    delta = str(params.get("delta") or "")
    if not delta:
        return str(state.get("streaming_text") or "")
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_id = _agent_message_item_id(params, item)
    items = _streaming_items(state)
    target = _find_streaming_item(items, item_id)
    if target is None and not item_id:
        target = _last_open_streaming_item(items)
    if target is None:
        target = {
            "id": item_id or _new_anonymous_streaming_item_id(state),
            "text": "",
            "completed": False,
        }
        items.append(target)
    if not bool(target.get("completed")):
        target["text"] = str(target.get("text") or "") + delta
    return _rebuild_streaming_text(state)


def reconcile_completed_agent_message(
    state: Dict[str, Any], params: Dict[str, Any], item: Dict[str, Any]
) -> str:
    text = str(item.get("text") or "")
    items = _streaming_items(state)
    item_id = _agent_message_item_id(params, item)
    target = _find_streaming_item(items, item_id)
    if target is None:
        open_item = _last_open_streaming_item(items)
        if not item_id:
            target = open_item
        elif open_item and str(open_item.get("id") or "").startswith("\x00anonymous-"):
            # CODEX: A legacy delta can arrive before its completion supplies the item ID.
            open_item["id"] = item_id
            target = open_item
    if target is None and text:
        target = {
            "id": item_id or _new_anonymous_streaming_item_id(state),
            "text": "",
            "completed": False,
        }
        items.append(target)
    if target is not None:
        if text:
            target["text"] = text
        target["completed"] = True
    if text:
        state["completed_agent_text"] = text
    return _rebuild_streaming_text(state)


def inflight_turn_display_text(state: Dict[str, Any]) -> str:
    final_text = str(state.get("final_text") or "")
    if final_text:
        return final_text
    streaming_text = str(state.get("streaming_text") or "")
    if streaming_text:
        return streaming_text
    return str(state.get("completed_agent_text") or "")


def record_state_activity(
    state: Dict[str, Any],
    *,
    stage: str,
    title: str,
    detail: str = "",
    level: str = "overview",
    status: str = "active",
    source: str = "manual",
    key: str = "",
) -> ActivitySnapshot:
    """Record local lifecycle updates in the same model as server activities."""
    snapshot = activity_feed_for_state(state).record(
        stage=stage,
        title=title,
        detail=detail,
        level=level,
        status=status,
        source=source,
        key=key,
    )
    present_activity_snapshot(state, snapshot)
    return snapshot


def format_duration(seconds: Any) -> str:
    """Format an elapsed duration without making minute-scale work ambiguous."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    value = max(0.0, value)
    rounded = int(round(value))
    if value >= 60:
        minutes, remainder = divmod(rounded, 60)
        return f"{minutes}m {remainder}s"
    if value.is_integer():
        return f"{int(value)}s"
    return f"{value:.1f}s"


def _nonnegative_token_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def format_token_usage(total_tokens: Any) -> str:
    """Format the turn's total processed token count for compact metadata."""
    total = _nonnegative_token_count(total_tokens) or 0
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M total tokens"
    if total >= 10_000:
        return f"{total / 1_000:.0f}k total tokens"
    return f"{total:,} total tokens"


def token_usage_tooltip(total_tokens: Any) -> str:
    total = _nonnegative_token_count(total_tokens) or 0
    return (
        f"{total:,} total tokens processed during this turn. Includes input, "
        "cached context, output, and every model/tool step; it is not only "
        "visible response or reasoning tokens."
    )


def _usage_breakdown_total(value: Any) -> Optional[int]:
    """Read one usage breakdown without double-counting cached/reasoning subsets."""
    if not isinstance(value, dict):
        return _nonnegative_token_count(value)
    for key in ("totalTokens", "total_tokens"):
        if key in value:
            total = _nonnegative_token_count(value.get(key))
            if total is not None:
                return total
    input_count = next(
        (
            _nonnegative_token_count(value.get(key))
            for key in ("inputTokens", "input_tokens")
            if key in value
        ),
        None,
    )
    output_count = next(
        (
            _nonnegative_token_count(value.get(key))
            for key in ("outputTokens", "output_tokens")
            if key in value
        ),
        None,
    )
    if input_count is None and output_count is None:
        return None
    # cached input is included in input, and reasoning output is included in
    # output. Adding either again would inflate the reported total.
    return (input_count or 0) + (output_count or 0)


def token_usage_snapshot(params: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Normalize current and legacy Codex usage notification shapes.

    The v2 event exposes a cumulative thread ``total`` plus the most recent
    upstream ``last`` breakdown. Legacy rollout-shaped payloads use
    ``total_token_usage``/``last_token_usage``. ``rawResponse/completed`` uses
    a direct ``usage`` breakdown for one response.
    """
    container: Any = None
    for key in ("tokenUsage", "token_usage", "tokenUsageInfo", "token_usage_info"):
        if isinstance(params.get(key), dict):
            container = params[key]
            break
    info = params.get("info")
    if container is None and isinstance(info, dict):
        container = info
    if not isinstance(container, dict):
        container = {}

    cumulative: Optional[int] = None
    for key in ("total", "totalTokenUsage", "total_token_usage"):
        if key in container:
            cumulative = _usage_breakdown_total(container.get(key))
            if cumulative is not None:
                break
    last: Optional[int] = None
    for key in ("last", "lastTokenUsage", "last_token_usage"):
        if key in container:
            last = _usage_breakdown_total(container.get(key))
            if last is not None:
                break

    direct: Optional[int] = None
    direct_candidates = [
        params.get("usage"),
        (params.get("turn") or {}).get("usage")
        if isinstance(params.get("turn"), dict)
        else None,
        container,
        params,
    ]
    for candidate in direct_candidates:
        total = _usage_breakdown_total(candidate)
        if total is not None:
            direct = total
            break
    return {"cumulative": cumulative, "last": last, "direct": direct}


def update_turn_token_usage(
    state: Dict[str, Any],
    params: Dict[str, Any],
    thread_totals: Dict[str, int],
    *,
    raw_response: bool = False,
) -> Optional[int]:
    """Apply a usage snapshot and return the authoritative turn-local total.

    Thread totals are snapshots, not deltas. The first event establishes a
    baseline and later events subtract that same baseline, so repeated or
    cumulative notifications are never added twice. Raw response usage is
    summed only once per response ID and remains a fallback until a canonical
    cumulative snapshot arrives.
    """
    snapshot = token_usage_snapshot(params)
    thread_id = str(
        params.get("threadId")
        or params.get("thread_id")
        or state.get("thread_id")
        or ""
    )
    cumulative = snapshot["cumulative"]
    last = snapshot["last"]
    direct = snapshot["direct"]

    if raw_response and direct is not None:
        response_id = str(params.get("responseId") or params.get("response_id") or "")
        if response_id:
            response_totals = state.setdefault("_token_response_totals", {})
            response_totals[response_id] = direct
            raw_total = sum(int(value) for value in response_totals.values())
        else:
            # Without a stable response ID, replacing is safer than summing an
            # event that may be replayed.
            raw_total = direct
        state["_token_raw_total"] = raw_total
        if not state.get("_token_usage_authoritative"):
            state["token_usage"] = raw_total
        return _nonnegative_token_count(state.get("token_usage"))

    if cumulative is not None:
        previous_thread_total = thread_totals.get(thread_id) if thread_id else None
        if (
            previous_thread_total is not None
            and cumulative < previous_thread_total
        ):
            # A delayed replay must not roll an active turn back to an older
            # cumulative snapshot.
            return _nonnegative_token_count(state.get("token_usage"))
        baseline = _nonnegative_token_count(state.get("_token_usage_baseline"))
        if baseline is None:
            if previous_thread_total is not None:
                baseline = previous_thread_total
            else:
                raw_total = _nonnegative_token_count(state.get("_token_raw_total"))
                first_turn_total = raw_total if raw_total is not None else last
                if first_turn_total is not None and cumulative >= first_turn_total:
                    baseline = cumulative - first_turn_total
        if thread_id:
            thread_totals[thread_id] = cumulative
        if baseline is None:
            # A bare lifetime total cannot safely be attributed to this turn.
            return _nonnegative_token_count(state.get("token_usage"))
        state["_token_usage_baseline"] = baseline
        state["_token_usage_authoritative"] = True
        state["token_usage"] = max(0, cumulative - baseline)
        return int(state["token_usage"])

    fallback = last if last is not None else direct
    if fallback is not None and not state.get("_token_usage_authoritative"):
        # This is a latest snapshot fallback, deliberately not ``+=``.
        state["token_usage"] = fallback
    return _nonnegative_token_count(state.get("token_usage"))


def composer_is_rtl(text: str) -> bool:
    """Prefer RTL for an empty draft or one containing strong RTL characters.

    User prompts commonly begin with a technical identifier such as ``Spark``
    and continue in an RTL language. Preserve that deliberate composer policy
    without mistaking accented Latin, Greek, CJK text, or emoji for RTL. The
    empty composer retains its RTL preference because the desktop keymap may
    not report an active RTL layout reliably.
    """
    value = text or ""
    return not value.strip() or any(
        unicodedata.bidirectional(character) in {"R", "AL"}
        for character in value
    )


def composer_paragraph_is_rtl(text: str) -> bool:
    return composer_is_rtl(text)


def conversation_markdown(room: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    """Build a portable, UTF-8 conversation export without touching the GUI."""
    lines = [f"# {room['title']}", ""]
    for message in messages:
        author = "You" if message["role"] == "user" else "Codex"
        timestamp = datetime.fromtimestamp(float(message["created_at"])).strftime(
            "%Y-%m-%d %H:%M"
        )
        lines.extend((f"## {author} — {timestamp}", ""))
        image_paths = list(message.get("image_paths") or [])
        if not image_paths and message.get("image_path"):
            image_paths = [str(message["image_path"])]
        for index, image_path in enumerate(image_paths, start=1):
            label = "Attached image" if len(image_paths) == 1 else f"Attached image {index}"
            lines.extend((f"![{label}](<{image_path}>)", ""))
        lines.extend((str(message.get("content") or "_Image message_"), ""))
    return "\n".join(lines)


def dispatch(callback: Any, *args: Any) -> int:
    return GLib.idle_add(callback, *args)


def icon_button(
    icon_name: str, tooltip: str, style_class: str = "icon-button"
) -> Gtk.Button:
    """Create a compact, keyboard-focusable symbolic icon button."""
    button = Gtk.Button()
    button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.get_style_context().add_class(style_class)
    button.get_accessible().set_name(tooltip)
    return button


class EmptyState(Gtk.Box):
    """A useful first-run state instead of a fake assistant message."""

    SUGGESTIONS = (
        "Review this project and flag the most important issues",
        "Debug this error step by step",
        "Explain the structure of this codebase",
    )

    def __init__(self, on_suggestion: Any) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(90)
        self.set_margin_bottom(60)
        self.get_style_context().add_class("empty-state")
        mark = Gtk.Label(label="⌁")
        mark.get_style_context().add_class("empty-mark")
        self.pack_start(mark, False, False, 0)
        title = Gtk.Label(label="What can I help you build?")
        title.get_style_context().add_class("empty-title")
        self.pack_start(title, False, False, 0)
        subtitle = Gtk.Label(label="Write a message, paste an image, or choose a prompt")
        subtitle.get_style_context().add_class("empty-subtitle")
        self.pack_start(subtitle, False, False, 0)
        suggestions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        for prompt in self.SUGGESTIONS:
            button = Gtk.Button.new_with_label(prompt)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class("suggestion-chip")
            button.connect("clicked", lambda _button, value=prompt: on_suggestion(value))
            suggestions.pack_start(button, False, False, 0)
        self.pack_start(suggestions, False, False, 6)


class RoomRow(Gtk.Box):
    """Sidebar room row with preview, state and explicit management menu."""

    def __init__(
        self,
        room_id: int,
        title: str,
        preview: str,
        on_select: Any,
        on_rename: Any,
        on_export: Any,
        on_delete: Any,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.set_direction(Gtk.TextDirection.LTR)
        self.get_style_context().add_class("room-row")
        self.room_id = room_id
        self.title = title
        self.main_button = Gtk.Button()
        self.main_button.set_relief(Gtk.ReliefStyle.NONE)
        self.main_button.set_hexpand(True)
        self.main_button.get_style_context().add_class("room-link")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.title_label = Gtk.Label(label=title)
        self.title_label.set_xalign(0.0)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.get_style_context().add_class("room-title")
        self.preview_label = Gtk.Label(label=preview)
        self.preview_label.set_xalign(0.0)
        self.preview_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.preview_label.get_style_context().add_class("room-preview")
        content.pack_start(self.title_label, False, False, 0)
        content.pack_start(self.preview_label, False, False, 0)
        self.main_button.add(content)
        self.main_button.connect("clicked", lambda _button: on_select(room_id))
        self.pack_start(self.main_button, True, True, 0)

        menu = Gtk.Menu()
        rename_item = Gtk.MenuItem.new_with_label("Rename")
        rename_item.connect("activate", lambda _item: on_rename(room_id))
        export_item = Gtk.MenuItem.new_with_label("Export Markdown")
        export_item.connect("activate", lambda _item: on_export(room_id))
        delete_item = Gtk.MenuItem.new_with_label("Delete chat")
        delete_item.get_style_context().add_class("danger-menu-item")
        delete_item.connect("activate", lambda _item: on_delete(room_id))
        menu.append(rename_item)
        menu.append(export_item)
        menu.append(delete_item)
        menu.show_all()
        more = Gtk.MenuButton()
        more.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.MENU))
        more.set_popup(menu)
        more.set_relief(Gtk.ReliefStyle.NONE)
        more.set_tooltip_text("Chat options")
        more.get_style_context().add_class("room-menu-button")
        self.pack_end(more, False, False, 0)

    def set_selected(self, selected: bool) -> None:
        context = self.main_button.get_style_context()
        (context.add_class if selected else context.remove_class)("active-room")
        row_context = self.get_style_context()
        (row_context.add_class if selected else row_context.remove_class)(
            "active-room-row"
        )

    def set_kind(self, kind: str) -> None:
        context = self.main_button.get_style_context()
        row_context = self.get_style_context()
        for name in (
            "busy-room",
            "stopping-room",
            "reconnecting-room",
            "unread-room",
        ):
            context.remove_class(name)
            row_context.remove_class(f"{name}-row")
        if kind:
            context.add_class(kind)
            row_context.add_class(f"{kind}-row")

    def set_status(self, text: str) -> None:
        self.preview_label.set_text(text)


class MessageBubble(Gtk.Box):
    def __init__(
        self,
        role: str,
        content: str,
        image_path: Optional[str] = None,
        streaming: bool = False,
        message_id: Optional[int] = None,
        reply_preview: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        on_reply: Optional[Any] = None,
        created_at: Optional[float] = None,
        token_usage: Optional[int] = None,
        on_branch: Optional[Any] = None,
        image_paths: Optional[List[str]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        # pack_start/pack_end are direction-aware.  Pinning the row itself to
        # LTR keeps user bubbles on the right and assistant bubbles on the left
        # even though the application default is RTL.
        self.set_direction(Gtk.TextDirection.LTR)
        self.role = role
        self.content = content
        self.message_id = message_id
        self.on_reply = on_reply
        self.on_branch = on_branch
        self.streaming = streaming
        self.selected_text = ""
        self.branch_panels: Dict[int, BranchPanel] = {}
        self.last_elapsed = int(duration_seconds or 0)
        self.get_style_context().add_class("message-row")

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.bubble = bubble
        bubble.get_style_context().add_class("message-bubble")
        bubble.get_style_context().add_class(
            "user-bubble" if role == "user" else "assistant-bubble"
        )
        bubble.set_size_request(120, -1)
        bubble.set_valign(Gtk.Align.START)

        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        identity.set_direction(Gtk.TextDirection.LTR)
        identity.get_style_context().add_class("message-identity")
        avatar = Gtk.Label(label="Y" if role == "user" else "C")
        avatar.get_style_context().add_class("message-avatar")
        author = Gtk.Label(label="You" if role == "user" else "Codex")
        author.get_style_context().add_class("message-author")
        author.set_direction(Gtk.TextDirection.LTR)
        timestamp = Gtk.Label()
        if created_at:
            timestamp.set_text(datetime.fromtimestamp(float(created_at)).strftime("%H:%M"))
        timestamp.get_style_context().add_class("message-time")
        identity.pack_start(avatar, False, False, 0)
        identity.pack_start(author, False, False, 0)
        identity.pack_start(timestamp, False, False, 0)
        bubble.pack_start(identity, False, False, 0)

        attached_paths = [str(path) for path in (image_paths or []) if path]
        if image_path and str(image_path) not in attached_paths:
            attached_paths.insert(0, str(image_path))
        if attached_paths:
            gallery = Gtk.Grid()
            gallery.set_row_spacing(7)
            gallery.set_column_spacing(7)
            gallery.set_row_homogeneous(True)
            gallery.set_column_homogeneous(True)
            gallery.set_halign(Gtk.Align.END if role == "user" else Gtk.Align.START)
            gallery.set_valign(Gtk.Align.START)
            gallery.get_style_context().add_class("message-image-gallery")
            columns = 1 if len(attached_paths) == 1 else 2
            tile_size = (420, 260) if columns == 1 else (196, 132)
            image_size = (412, 252) if columns == 1 else (188, 124)
            rendered_images = 0
            for path in attached_paths:
                image_name = Path(path).name or "Attached image"
                tile = Gtk.Button()
                tile.set_relief(Gtk.ReliefStyle.NONE)
                tile.set_size_request(tile_size[0], tile_size[1])
                tile.set_halign(Gtk.Align.FILL)
                tile.set_valign(Gtk.Align.FILL)
                tile.get_style_context().add_class("message-image-tile")
                gallery.attach(
                    tile,
                    rendered_images % columns,
                    rendered_images // columns,
                    1,
                    1,
                )
                rendered_images += 1
                try:
                    inspect_attachment_image(path)
                    tile.set_tooltip_text(f"Attached image: {image_name}")
                    tile.connect(
                        "clicked",
                        self._show_message_image_preview,
                        path,
                        image_name,
                    )
                    loading = Gtk.Spinner()
                    loading.start()
                    loading.get_style_context().add_class("message-image-loading")
                    loading.set_halign(Gtk.Align.CENTER)
                    loading.set_valign(Gtk.Align.CENTER)
                    tile.add(loading)
                    load_attachment_image_async(
                        path,
                        image_size[0],
                        image_size[1],
                        lambda _info, pixbuf, target=tile, name=image_name: self._set_message_image_tile(
                            target, name, pixbuf, require_mounted=True
                        ),
                        lambda _error, target=tile, name=image_name: self._set_missing_message_image_tile(
                            target, name, require_mounted=True
                        ),
                    )
                except (AttachmentImageError, GLib.Error, OSError):
                    self._set_missing_message_image_tile(tile, image_name)
                accessible = tile.get_accessible()
                if accessible is not None:
                    accessible.set_name(tile.get_tooltip_text())
            bubble.pack_start(gallery, False, False, 0)

        if reply_preview:
            reply = Gtk.Label(label=reply_preview[:180])
            reply.set_line_wrap(True)
            reply.set_max_width_chars(68)
            reply_is_rtl = content_prefers_rtl(reply_preview)
            reply.set_xalign(1.0 if reply_is_rtl else 0.0)
            reply.set_direction(
                Gtk.TextDirection.RTL if reply_is_rtl else Gtk.TextDirection.LTR
            )
            reply.get_style_context().add_class("reply-quote")
            bubble.pack_start(reply, False, False, 0)

        self.content_view = MarkdownView(
            content or "",
            user_message=role == "user",
        )
        bubble.pack_start(self.content_view, False, False, 0)
        if not content:
            self.content_view.set_no_show_all(True)
            self.content_view.hide()
        self.selection_context_menu: Optional[Gtk.Menu] = None
        self.selection_reply_item: Optional[Gtk.MenuItem] = None
        self.selection_branch_item: Optional[Gtk.MenuItem] = None
        self.selection_copy_item: Optional[Gtk.MenuItem] = None
        self.selection_clear_item: Optional[Gtk.MenuItem] = None
        self.content_view.connect_selection_changed(self._selection_changed)
        self.content_view.connect_selection_context_requested(
            self._show_selection_context_menu
        )

        self.activity_revealer: Optional[Gtk.Revealer] = None
        self.activity_box: Optional[Gtk.Box] = None
        self.activity_label: Optional[Gtk.Label] = None
        self.activity_detail: Optional[Gtk.Label] = None
        self.activity_history: List[str] = []
        self.activity_timeline: Optional[Gtk.Label] = None
        self.activity_snapshot: Optional[Any] = None
        self.activity_spinner: Optional[Gtk.Spinner] = None
        self.activity_state_label: Optional[Gtk.Label] = None
        self.activity_summary_label: Optional[Gtk.Label] = None
        self.activity_summary_meta: Optional[Gtk.Label] = None
        self.activity_stage_flow: Optional[Gtk.FlowBox] = None
        self.activity_detail_stack: Optional[Gtk.Stack] = None
        self.activity_steps_box: Optional[Gtk.Box] = None
        self.activity_trace_box: Optional[Gtk.Box] = None
        self.activity_level_buttons: Dict[str, Gtk.Button] = {}
        self.activity_expander: Optional[Gtk.Expander] = None
        self._activity_detail_level = "overview"
        self._activity_update_count = 0
        self._activity_has_updates = False
        self._activity_presentation = "working"
        if role == "assistant" and streaming:
            self.activity_revealer = Gtk.Revealer()
            self.activity_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            activity_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            self.activity_box = activity_box
            activity_box.get_style_context().add_class("activity-log")
            activity_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            activity_head.set_direction(Gtk.TextDirection.LTR)
            self.activity_spinner = Gtk.Spinner()
            self.activity_spinner.get_style_context().add_class("activity-spinner")
            self.activity_spinner.start()
            activity_head.pack_start(self.activity_spinner, False, False, 0)
            self.activity_label = Gtk.Label(label="Working on your request")
            self.activity_label.set_line_wrap(True)
            self.activity_label.set_xalign(0.0)
            self.activity_label.set_direction(Gtk.TextDirection.LTR)
            self.activity_label.get_style_context().add_class("activity-headline")
            activity_head.pack_start(self.activity_label, True, True, 0)
            self.activity_state_label = Gtk.Label(label="LIVE")
            self.activity_state_label.set_xalign(1.0)
            self.activity_state_label.set_direction(Gtk.TextDirection.LTR)
            self.activity_state_label.get_style_context().add_class("activity-state")
            activity_head.pack_end(self.activity_state_label, False, False, 0)
            activity_box.pack_start(activity_head, False, False, 0)

            self.activity_detail = Gtk.Label(label="")
            self.activity_detail.set_selectable(False)
            self.activity_detail.set_line_wrap(True)
            self.activity_detail.set_xalign(0.0)
            self.activity_detail.set_direction(Gtk.TextDirection.LTR)
            self.activity_detail.get_style_context().add_class("activity-current-detail")
            self.activity_detail.set_ellipsize(Pango.EllipsizeMode.END)
            activity_box.pack_start(self.activity_detail, False, False, 0)

            self.activity_stage_flow = Gtk.FlowBox()
            self.activity_stage_flow.set_selection_mode(Gtk.SelectionMode.NONE)
            self.activity_stage_flow.set_homogeneous(False)
            self.activity_stage_flow.set_min_children_per_line(1)
            self.activity_stage_flow.set_max_children_per_line(6)
            self.activity_stage_flow.set_row_spacing(4)
            self.activity_stage_flow.set_column_spacing(5)
            self.activity_stage_flow.set_halign(Gtk.Align.START)
            self.activity_stage_flow.get_style_context().add_class("activity-stage-flow")
            activity_box.pack_start(self.activity_stage_flow, False, False, 0)

            self.activity_expander = Gtk.Expander(label="Activity details")
            self.activity_expander.set_direction(Gtk.TextDirection.LTR)
            self.activity_expander.get_style_context().add_class("activity-expander")
            activity_details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            activity_details.get_style_context().add_class("activity-details-panel")
            level_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            level_bar.set_direction(Gtk.TextDirection.LTR)
            level_bar.get_style_context().add_class("activity-detail-levels")
            for level, label in (
                ("overview", "Overview"),
                ("steps", "Steps"),
                ("trace", "Trace"),
            ):
                button = Gtk.Button.new_with_label(label)
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.get_style_context().add_class("activity-detail-level")
                button.connect(
                    "clicked",
                    lambda _button, selected_level=level: self._set_activity_detail_level(
                        selected_level
                    ),
                )
                self.activity_level_buttons[level] = button
                level_bar.pack_start(button, False, False, 0)
            activity_details.pack_start(level_bar, False, False, 0)

            self.activity_detail_stack = Gtk.Stack()
            self.activity_detail_stack.set_transition_type(
                Gtk.StackTransitionType.CROSSFADE
            )
            self.activity_detail_stack.set_hhomogeneous(False)
            self.activity_detail_stack.set_vhomogeneous(False)
            self.activity_detail_stack.set_vexpand(False)
            self.activity_detail_stack.get_style_context().add_class("activity-detail-stack")
            summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            summary_box.get_style_context().add_class("activity-detail-section")
            self.activity_summary_label = Gtk.Label(label="Waiting for the first update…")
            self.activity_summary_label.set_line_wrap(True)
            self.activity_summary_label.set_xalign(0.0)
            self.activity_summary_label.set_direction(Gtk.TextDirection.LTR)
            self.activity_summary_label.get_style_context().add_class("activity-summary")
            summary_box.pack_start(self.activity_summary_label, False, False, 0)
            self.activity_summary_meta = Gtk.Label(label="")
            self.activity_summary_meta.set_xalign(0.0)
            self.activity_summary_meta.set_direction(Gtk.TextDirection.LTR)
            self.activity_summary_meta.get_style_context().add_class("activity-summary-meta")
            summary_box.pack_start(self.activity_summary_meta, False, False, 0)
            self.activity_detail_stack.add_named(summary_box, "overview")

            self.activity_steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            self.activity_steps_box.get_style_context().add_class("activity-detail-section")
            self.activity_detail_stack.add_named(self.activity_steps_box, "steps")

            self.activity_trace_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            self.activity_trace_box.get_style_context().add_class("activity-detail-section")
            self.activity_detail_stack.add_named(self.activity_trace_box, "trace")
            activity_details.pack_start(self.activity_detail_stack, False, False, 0)
            self.activity_expander.add(activity_details)
            activity_box.pack_start(self.activity_expander, False, False, 0)
            self.activity_revealer.add(activity_box)
            self.activity_revealer.set_reveal_child(True)
            bubble.pack_start(self.activity_revealer, False, False, 0)
            self._set_activity_detail_level("overview")

        self.timer_label: Optional[Gtk.Label] = None
        if streaming:
            self.timer_label = Gtk.Label(label="Running • 0s")
            self.timer_label.set_xalign(0.0)
            self.timer_label.get_style_context().add_class("turn-timer")
            bubble.pack_start(self.timer_label, False, False, 0)

        metadata = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        metadata.set_direction(Gtk.TextDirection.LTR)
        metadata.get_style_context().add_class("message-meta-row")
        self.duration_label = Gtk.Label()
        self.duration_label.set_xalign(0.0)
        self.duration_label.get_style_context().add_class("message-meta")
        if duration_seconds is not None and role == "assistant":
            self.duration_label.set_text(f"Thought for {format_duration(duration_seconds)}")
        metadata.pack_start(self.duration_label, False, False, 0)
        self.usage_label = Gtk.Label()
        self.usage_label.get_style_context().add_class("message-meta")
        if token_usage is not None:
            self.set_token_usage(int(token_usage))
        metadata.pack_start(self.usage_label, False, False, 0)
        if role == "assistant":
            bubble.pack_start(metadata, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.get_style_context().add_class("message-actions")
        footer.set_direction(Gtk.TextDirection.LTR)
        self.copy_button = Gtk.Button.new_with_label("Copy")
        self.copy_button.set_relief(Gtk.ReliefStyle.NONE)
        self.copy_button.get_style_context().add_class("message-action")
        self.copy_button.set_no_show_all(streaming)

        def copy_message(button: Gtk.Button) -> None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.content, -1)
            button.set_label("Copied ✓")
            GLib.timeout_add(1400, lambda: self._reset_copy_label(button))

        self.copy_button.connect("clicked", copy_message)
        footer.pack_end(self.copy_button, False, False, 0)
        self.reply_button = Gtk.Button.new_with_label("↩ Reply")
        self.reply_button.set_relief(Gtk.ReliefStyle.NONE)
        self.reply_button.get_style_context().add_class("message-action")
        self.reply_button.set_no_show_all(
            streaming or on_reply is None or message_id is None
        )
        if on_reply:
            self.reply_button.connect(
                "clicked", lambda _button: on_reply(self.message_id, role, self.content)
            )
        else:
            self.reply_button.set_sensitive(False)
        footer.pack_end(self.reply_button, False, False, 0)
        self.branch_toggle_button = Gtk.Button.new_with_label("Side threads")
        self.branch_toggle_button.set_relief(Gtk.ReliefStyle.NONE)
        self.branch_toggle_button.get_style_context().add_class("message-action")
        self.branch_toggle_button.set_no_show_all(True)
        self.branch_toggle_button.connect("clicked", self._toggle_branches)
        footer.pack_end(self.branch_toggle_button, False, False, 0)
        bubble.pack_start(footer, False, False, 0)

        self.branch_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10
        )
        self.branch_box.set_direction(Gtk.TextDirection.LTR)
        self.branch_box.set_no_show_all(True)
        bubble.pack_start(self.branch_box, False, False, 0)

        if role == "user":
            self.pack_end(bubble, False, False, 0)
        else:
            bubble.set_hexpand(True)
            self.pack_start(bubble, True, True, 0)

    def _selection_changed(self, _view: MarkdownView, text: str) -> None:
        self.selected_text = text.strip()
        context = self.bubble.get_style_context()
        (context.add_class if self.selected_text else context.remove_class)(
            "has-text-selection"
        )

    def _build_selection_context_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.set_direction(Gtk.TextDirection.LTR)
        menu.get_style_context().add_class("selection-context-menu")

        self.selection_reply_item = Gtk.MenuItem.new_with_label(
            "Reply to selection"
        )
        self.selection_reply_item.get_style_context().add_class(
            "selection-context-item"
        )
        self.selection_reply_item.set_sensitive(
            bool(
                self.selected_text
                and self.message_id is not None
                and not self.streaming
                and self.on_reply is not None
            )
        )
        self.selection_reply_item.connect("activate", self._reply_to_selection)
        menu.append(self.selection_reply_item)

        self.selection_branch_item = None
        if self.role == "assistant" and self.on_branch is not None:
            self.selection_branch_item = Gtk.MenuItem.new_with_label(
                "Ask in side thread"
            )
            self.selection_branch_item.get_style_context().add_class(
                "selection-context-item"
            )
            self.selection_branch_item.set_sensitive(
                bool(
                    self.selected_text
                    and self.message_id is not None
                    and not self.streaming
                )
            )
            self.selection_branch_item.connect(
                "activate", self._branch_from_selection
            )
            menu.append(self.selection_branch_item)

        menu.append(Gtk.SeparatorMenuItem())
        self.selection_copy_item = Gtk.MenuItem.new_with_label("Copy selection")
        self.selection_copy_item.connect("activate", self._copy_selection)
        menu.append(self.selection_copy_item)
        self.selection_clear_item = Gtk.MenuItem.new_with_label("Clear selection")
        self.selection_clear_item.connect(
            "activate", lambda _item: self.content_view.clear_selection()
        )
        menu.append(self.selection_clear_item)
        return menu

    def _show_selection_context_menu(
        self,
        _view: MarkdownView,
        selected_text: str,
        event: Optional[object],
        source_widget: Gtk.Widget,
        _root_x: float,
        _root_y: float,
    ) -> None:
        self.selected_text = selected_text.strip()
        if not self.selected_text:
            return
        if self.selection_context_menu is not None:
            self.selection_context_menu.destroy()
        menu = self._build_selection_context_menu()
        self.selection_context_menu = menu
        menu.attach_to_widget(source_widget, None)
        menu.show_all()
        trigger_event = event or Gtk.get_current_event()
        if event is not None and hasattr(menu, "popup_at_pointer"):
            menu.popup_at_pointer(event)
        elif hasattr(menu, "popup_at_widget"):
            menu.popup_at_widget(
                source_widget,
                Gdk.Gravity.SOUTH_WEST,
                Gdk.Gravity.NORTH_WEST,
                trigger_event,
            )
        else:  # pragma: no cover - only for older GTK 3 releases
            menu.popup(
                None,
                None,
                None,
                None,
                0,
                Gdk.CURRENT_TIME,
            )

    def _copy_selection(self, _item: Gtk.MenuItem) -> None:
        selected = self.selected_text
        if selected:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(selected, -1)

    def _reply_to_selection(self, _item: Gtk.MenuItem) -> None:
        selected = self.selected_text
        if not selected or self.message_id is None or self.on_reply is None:
            return
        self.content_view.clear_selection()
        self.on_reply(self.message_id, self.role, selected)

    def _branch_from_selection(self, _item: Gtk.MenuItem) -> None:
        selected = self.selected_text
        if (
            not selected
            or self.role != "assistant"
            or self.message_id is None
            or self.on_branch is None
        ):
            return
        self.content_view.clear_selection()
        self.on_branch(self.message_id, selected, self)

    def add_branch_panel(self, panel: BranchPanel) -> None:
        branch_id = int(panel.branch_id)
        existing = self.branch_panels.get(branch_id)
        if existing is not None:
            existing.show_all()
            self.branch_box.show()
            return
        self.branch_panels[branch_id] = panel
        self.branch_box.pack_start(panel, False, False, 0)
        self.branch_box.set_no_show_all(False)
        self.branch_box.show_all()
        self.branch_toggle_button.set_label(
            f"Side threads · {len(self.branch_panels)}"
        )
        self.branch_toggle_button.set_no_show_all(False)
        self.branch_toggle_button.show()

    def collapse_branch(self, branch_id: int, panel: BranchPanel) -> None:
        if self.branch_panels.get(int(branch_id)) is panel:
            panel.hide()
        if not any(item.get_visible() for item in self.branch_panels.values()):
            self.branch_box.hide()

    def _toggle_branches(self, _button: Gtk.Button) -> None:
        if self.branch_box.get_visible():
            self.branch_box.hide()
            return
        for panel in self.branch_panels.values():
            panel.show_all()
        self.branch_box.show()

    @staticmethod
    def _reset_copy_label(button: Gtk.Button) -> bool:
        if button.get_parent() is not None:
            button.set_label("Copy")
        return False

    def set_text(self, value: str) -> None:
        self.content = value
        if not value:
            self.content_view.set_no_show_all(True)
            self.content_view.hide()
            return
        self.content_view.set_text(value)
        self.content_view.set_no_show_all(False)
        self.content_view.show()

    def _replace_message_image_tile_child(
        self,
        tile: Gtk.Button,
        child: Gtk.Widget,
        require_mounted: bool = False,
    ) -> bool:
        if tile.get_parent() is None or (
            require_mounted and self.get_parent() is None
        ):
            return False
        existing = tile.get_child()
        if existing is not None:
            tile.remove(existing)
        tile.add(child)
        return True

    def _set_message_image_tile(
        self,
        tile: Gtk.Button,
        image_name: str,
        pixbuf: GdkPixbuf.Pixbuf,
        require_mounted: bool = False,
    ) -> None:
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.get_style_context().add_class("message-image")
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        if not self._replace_message_image_tile_child(
            tile, image, require_mounted=require_mounted
        ):
            return
        context = tile.get_style_context()
        context.remove_class("message-image-tile-missing")
        tile.set_sensitive(True)
        tile.set_tooltip_text(f"Attached image: {image_name}")
        accessible = tile.get_accessible()
        if accessible is not None:
            accessible.set_name(tile.get_tooltip_text())
        tile.show_all()

    def _set_missing_message_image_tile(
        self,
        tile: Gtk.Button,
        image_name: str,
        require_mounted: bool = False,
    ) -> None:
        missing = Gtk.Image.new_from_icon_name(
            "image-missing", Gtk.IconSize.DIALOG
        )
        missing.get_style_context().add_class("message-image-missing")
        missing.set_halign(Gtk.Align.CENTER)
        missing.set_valign(Gtk.Align.CENTER)
        if not self._replace_message_image_tile_child(
            tile, missing, require_mounted=require_mounted
        ):
            return
        tile.get_style_context().add_class("message-image-tile-missing")
        tile.set_tooltip_text(f"Image unavailable: {image_name}")
        tile.set_sensitive(False)
        accessible = tile.get_accessible()
        if accessible is not None:
            accessible.set_name(tile.get_tooltip_text())
        tile.show_all()

    @staticmethod
    def _show_message_image_preview(
        tile: Gtk.Widget, image_path: str, image_name: str
    ) -> None:
        window = tile.get_toplevel()
        if not isinstance(window, Gtk.Window):
            return
        try:
            show_attachment_preview(window, image_path, image_name)
        except (AttachmentImageError, GLib.Error, OSError):
            tile.set_tooltip_text(f"Image unavailable: {image_name}")

    def set_elapsed(self, seconds: int, stopping: bool = False) -> None:
        if self.timer_label:
            self.last_elapsed = max(0, seconds)
            presentation = getattr(self, "_activity_presentation", "working")
            if stopping or presentation == "stopping":
                status = "Stopping"
            elif presentation == "reconnecting":
                status = "Reconnecting"
            elif presentation == "restoring":
                status = "Restoring"
            else:
                status = "Running"
            self.timer_label.set_text(f"{status} • {format_duration(seconds)}")

    def set_activity_presentation(self, presentation: str) -> None:
        normalized = str(presentation or "working").strip().lower()
        if normalized not in {
            "working",
            "reconnecting",
            "restoring",
            "stopping",
            "complete",
        }:
            normalized = "working"
        self._activity_presentation = normalized
        if self.activity_spinner:
            if normalized in {"working", "restoring"}:
                self.activity_spinner.start()
            else:
                self.activity_spinner.stop()
        if self.activity_state_label:
            labels = {
                "working": "LIVE",
                "reconnecting": "RECONNECTING",
                "restoring": "RESTORING",
                "stopping": "STOPPING",
                "complete": "DONE",
            }
            self.activity_state_label.set_text(labels[normalized])
            context = self.activity_state_label.get_style_context()
            for class_name in (
                "activity-state-complete",
                "activity-state-paused",
                "activity-state-stopping",
            ):
                context.remove_class(class_name)
            if normalized == "complete":
                context.add_class("activity-state-complete")
            elif normalized == "reconnecting":
                context.add_class("activity-state-paused")
            elif normalized == "stopping":
                context.add_class("activity-state-stopping")
        if self.activity_box:
            context = self.activity_box.get_style_context()
            for class_name in (
                "activity-complete",
                "activity-paused",
                "activity-stopping",
            ):
                context.remove_class(class_name)
            if normalized == "complete":
                context.add_class("activity-complete")
            elif normalized == "reconnecting":
                context.add_class("activity-paused")
            elif normalized == "stopping":
                context.add_class("activity-stopping")
        if self.timer_label:
            self.set_elapsed(self.last_elapsed, normalized == "stopping")

    def set_token_usage(self, total_tokens: int) -> None:
        total = max(0, int(total_tokens))
        self.usage_label.set_text(format_token_usage(total))
        self.usage_label.set_tooltip_text(token_usage_tooltip(total))

    @staticmethod
    def _activity_value(source: Any, name: str, default: Any = None) -> Any:
        if source is None or isinstance(source, (str, bytes)):
            return default
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    @staticmethod
    def _activity_text(value: Any, limit: int = 260) -> str:
        if value is None:
            return ""
        return clean_activity_text(str(value), limit)

    def _activity_entry_payload(
        self, entry: Any, default_level: str = "detail"
    ) -> Dict[str, Any]:
        if isinstance(entry, (str, bytes)):
            return {
                "title": self._activity_text(entry),
                "detail": "",
                "stage": "",
                "status": "",
                "level": default_level,
                "count": 1,
            }
        stage = self._activity_text(
            self._activity_value(entry, "stage")
            or self._activity_value(entry, "name"),
            80,
        )
        title = self._activity_text(
            self._activity_value(entry, "title")
            or self._activity_value(entry, "summary")
            or self._activity_value(entry, "text"),
            180,
        )
        detail = self._activity_text(
            self._activity_value(entry, "detail")
            or self._activity_value(entry, "description"),
            300,
        )
        if not title:
            title = stage
        if detail == title:
            detail = ""
        try:
            count = max(1, int(self._activity_value(entry, "count", 1)))
        except (TypeError, ValueError):
            count = 1
        return {
            "title": title,
            "detail": detail,
            "stage": stage,
            "status": self._activity_text(
                self._activity_value(entry, "status"), 32
            ).lower(),
            "level": self._activity_text(
                self._activity_value(entry, "level", default_level), 24
            ).lower()
            or default_level,
            "count": count,
        }

    def _activity_entries(self, snapshot: Any, name: str) -> List[Dict[str, Any]]:
        values = self._activity_value(snapshot, name, [])
        if isinstance(values, (str, bytes)) or values is None:
            values = []
        try:
            return [
                payload
                for payload in (
                    self._activity_entry_payload(value) for value in values
                )
                if payload["title"] or payload["detail"] or payload["stage"]
            ]
        except TypeError:
            return []

    def _activity_stages(self, snapshot: Any) -> List[Dict[str, Any]]:
        values = self._activity_value(snapshot, "stages", [])
        if isinstance(values, (str, bytes)) or values is None:
            return []
        stages: List[Dict[str, Any]] = []
        try:
            for value in values:
                payload = self._activity_entry_payload(value, "overview")
                if not payload["title"]:
                    payload["title"] = payload["stage"]
                if payload["stage"] or payload["title"]:
                    stages.append(payload)
        except TypeError:
            return []
        return stages

    @staticmethod
    def _set_activity_label(label: Optional[Gtk.Label], value: str) -> None:
        if label is None:
            return
        label.set_text(value)
        rtl = content_prefers_rtl(value)
        label.set_direction(Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR)
        label.set_xalign(1.0 if rtl else 0.0)

    @staticmethod
    def _activity_plural(value: int, singular: str, plural: Optional[str] = None) -> str:
        suffix = singular if value == 1 else (plural or f"{singular}s")
        return f"{value} {suffix}"

    def _set_activity_detail_level(self, level: str) -> None:
        if level not in self.activity_level_buttons:
            return
        self._activity_detail_level = level
        if self.activity_detail_stack:
            self.activity_detail_stack.set_visible_child_name(level)
        for name, button in self.activity_level_buttons.items():
            context = button.get_style_context()
            if name == level:
                context.add_class("activity-detail-level-active")
            else:
                context.remove_class("activity-detail-level-active")

    def _replace_activity_rows(
        self,
        container: Optional[Gtk.Box],
        entries: List[Dict[str, Any]],
        empty_text: str,
    ) -> None:
        if container is None:
            return
        for child in container.get_children():
            container.remove(child)
        if not entries:
            empty = Gtk.Label(label=empty_text)
            empty.set_xalign(0.0)
            empty.set_direction(Gtk.TextDirection.LTR)
            empty.get_style_context().add_class("activity-empty-detail")
            container.pack_start(empty, False, False, 0)
            empty.show()
            return
        for entry in entries[-10:]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_direction(Gtk.TextDirection.LTR)
            row.get_style_context().add_class("activity-entry-row")
            status = str(entry.get("status") or "").lower()
            marker_text = "•"
            marker_class = "activity-entry-pending"
            if status in {"complete", "completed", "done", "success", "finished"}:
                marker_text = "✓"
                marker_class = "activity-entry-complete"
            elif status in {"failed", "error", "cancelled", "canceled"}:
                marker_text = "!"
                marker_class = "activity-entry-error"
            elif status in {"active", "running", "in_progress", "in-progress"}:
                marker_text = "●"
                marker_class = "activity-entry-active"
            marker = Gtk.Label(label=marker_text)
            marker.get_style_context().add_class("activity-entry-marker")
            marker.get_style_context().add_class(marker_class)
            row.pack_start(marker, False, False, 0)

            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            copy.set_hexpand(True)
            title = str(entry.get("title") or entry.get("stage") or "Activity update")
            title_label = Gtk.Label(label=title)
            title_label.set_line_wrap(True)
            title_label.set_xalign(0.0)
            title_label.set_direction(
                Gtk.TextDirection.RTL if content_prefers_rtl(title) else Gtk.TextDirection.LTR
            )
            title_label.get_style_context().add_class("activity-entry-title")
            copy.pack_start(title_label, False, False, 0)
            detail = str(entry.get("detail") or "")
            if detail:
                detail_label = Gtk.Label(label=detail)
                detail_label.set_line_wrap(True)
                detail_label.set_xalign(0.0)
                detail_label.set_direction(
                    Gtk.TextDirection.RTL
                    if content_prefers_rtl(detail)
                    else Gtk.TextDirection.LTR
                )
                detail_label.get_style_context().add_class("activity-entry-detail")
                copy.pack_start(detail_label, False, False, 0)
            row.pack_start(copy, True, True, 0)
            count = int(entry.get("count") or 1)
            if count > 1:
                count_label = Gtk.Label(label=f"×{count}")
                count_label.get_style_context().add_class("activity-entry-count")
                row.pack_end(count_label, False, False, 0)
            container.pack_start(row, False, False, 0)
            row.show_all()

    def _render_activity_stages(self, stages: List[Dict[str, Any]]) -> None:
        if self.activity_stage_flow is None:
            return
        for child in self.activity_stage_flow.get_children():
            self.activity_stage_flow.remove(child)
        visible_stages = stages[-6:]
        for index, entry in enumerate(visible_stages):
            name = str(entry.get("stage") or entry.get("title") or "Working")
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            chip.set_direction(Gtk.TextDirection.LTR)
            chip.get_style_context().add_class("activity-stage-chip")
            status = str(entry.get("status") or "").lower()
            if status in {"complete", "completed", "done", "success", "finished"}:
                chip.get_style_context().add_class("activity-stage-complete")
            elif status in {"active", "running", "in_progress", "in-progress"} or index == len(visible_stages) - 1:
                chip.get_style_context().add_class("activity-stage-active")
            else:
                chip.get_style_context().add_class("activity-stage-pending")
            dot = Gtk.Label(label="●")
            dot.get_style_context().add_class("activity-stage-dot")
            name_label = Gtk.Label(label=name)
            name_label.set_ellipsize(Pango.EllipsizeMode.END)
            name_label.set_max_width_chars(22)
            name_label.get_style_context().add_class("activity-stage-name")
            chip.pack_start(dot, False, False, 0)
            chip.pack_start(name_label, False, False, 0)
            self.activity_stage_flow.add(chip)
            chip.show_all()
        self.activity_stage_flow.set_visible(bool(visible_stages))

    def set_activity_snapshot(self, snapshot: Any) -> None:
        if self.activity_label is None:
            return
        self.activity_snapshot = snapshot
        overview_source = self._activity_value(snapshot, "overview_entry")
        if overview_source is None:
            overview_source = self._activity_value(snapshot, "overview")
        overview = self._activity_entry_payload(overview_source, "overview")
        active_stage = self._activity_text(
            self._activity_value(snapshot, "active_stage"), 80
        )
        if not overview["stage"]:
            overview["stage"] = active_stage
        stages = self._activity_stages(snapshot)
        step_entries = self._activity_entries(snapshot, "details")
        trace_entries = self._activity_entries(snapshot, "trace")
        if not step_entries and not trace_entries:
            fallback_entries = self._activity_entries(snapshot, "entries")
            step_entries = [
                entry for entry in fallback_entries if entry.get("level") != "trace"
            ]
            trace_entries = [
                entry for entry in fallback_entries if entry.get("level") == "trace"
            ]
        entries = step_entries + trace_entries
        if not stages and (overview["stage"] or overview["title"]):
            stages = [overview]
        if not entries and (overview["title"] or overview["detail"]):
            entries = [overview]

        headline = overview["title"] or overview["stage"] or "Working on your request"
        detail = overview["detail"]
        if not detail and overview["stage"] and overview["stage"] != headline:
            detail = overview["stage"]
        self._set_activity_label(self.activity_label, headline)
        self._set_activity_label(self.activity_detail, detail)
        if self.activity_detail:
            self.activity_detail.set_no_show_all(not bool(detail))
            self.activity_detail.set_visible(bool(detail))
        if self.activity_stage_flow:
            self.activity_stage_flow.set_no_show_all(False)
        self._render_activity_stages(stages)

        supplied_count = self._activity_value(snapshot, "update_count")
        if supplied_count is None:
            supplied_count = self._activity_value(snapshot, "count")
        try:
            count = max(0, int(supplied_count))
        except (TypeError, ValueError):
            count = sum(int(entry.get("count") or 1) for entry in entries)
            if not count:
                count = int(overview.get("count") or 0)
        self._activity_update_count = max(self._activity_update_count, count)
        self._activity_has_updates = bool(entries or stages or detail)
        self.activity_history = [
            str(entry.get("title") or entry.get("stage") or "") for entry in entries[-10:]
        ]
        if self.activity_summary_label:
            summary = detail or headline
            if overview["stage"] and overview["stage"] != headline:
                summary = f"{overview['stage']} · {summary}"
            self._set_activity_label(self.activity_summary_label, summary)
        if self.activity_summary_meta:
            stage_count = len(stages)
            update_count = self._activity_update_count
            self.activity_summary_meta.set_text(
                f"{self._activity_plural(stage_count, 'stage')} · "
                f"{self._activity_plural(update_count, 'update')}"
            )
        self._replace_activity_rows(
            self.activity_steps_box,
            step_entries,
            "No task steps have been recorded yet.",
        )
        self._replace_activity_rows(
            self.activity_trace_box,
            trace_entries,
            "No low-level activity has been recorded yet.",
        )
        if self.activity_expander:
            self.activity_expander.set_label(
                f"Activity details · {self._activity_plural(self._activity_update_count, 'update')}"
            )
            self.activity_expander.show()
        presentation = getattr(self, "_activity_presentation", "working")
        status = str(overview.get("status") or "").lower()
        if presentation not in {"stopping", "complete"} and status in {
            "warning",
            "paused",
            "reconnecting",
            "disconnected",
        }:
            presentation = "reconnecting"
        elif presentation == "reconnecting" and status in {
            "active",
            "running",
            "pending",
        }:
            presentation = "working"
        if presentation not in {"stopping", "complete"}:
            self.set_activity_presentation(presentation)
        if self.activity_revealer:
            self.activity_revealer.set_reveal_child(True)

    def set_activity(self, value: str) -> None:
        if not self.activity_label or not value:
            return
        legacy = self._activity_text(value.replace("*", " ").replace("`", " "), 260)
        title, separator, detail = legacy.partition(": ")
        overview = {
            "title": title or "Working on your request",
            "detail": detail if separator else "",
            "stage": title if separator else "Working",
            "status": "active",
            "level": "overview",
        }
        self.set_activity_snapshot(
            {"overview": overview, "stages": [overview], "entries": [overview]}
        )

    def hide_timer(self, duration_seconds: Optional[float] = None) -> None:
        if self.timer_label:
            self.timer_label.hide()
        duration = float(duration_seconds if duration_seconds is not None else self.last_elapsed)
        if self.activity_revealer:
            self.activity_revealer.set_reveal_child(True)
        if self.activity_label:
            update_text = (
                "no activity updates"
                if self._activity_update_count == 0
                else self._activity_plural(self._activity_update_count, "update")
            )
            self.activity_label.set_text(
                f"Worked for {format_duration(duration)} · "
                f"{update_text}"
            )
            self.activity_label.set_direction(Gtk.TextDirection.LTR)
            self.activity_label.set_xalign(0.0)
        if self.activity_detail:
            self.activity_detail.set_no_show_all(True)
            self.activity_detail.hide()
        if self.activity_stage_flow:
            self.activity_stage_flow.set_no_show_all(True)
            self.activity_stage_flow.hide()
        self.set_activity_presentation("complete")
        if self.activity_expander:
            self.activity_expander.set_expanded(False)
            self.activity_expander.show()
        if self.role == "assistant":
            self.duration_label.set_text(f"Thought for {format_duration(duration)}")
        self.streaming = False
        self._selection_changed(self.content_view, self.content_view.get_selected_text())
        self.reply_button.set_no_show_all(False)
        self.reply_button.show()
        self.copy_button.set_no_show_all(False)
        self.copy_button.show()


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1120, 760)
        self.set_size_request(780, 560)
        self.set_wmclass("codex", "Codex")
        if ICON_PATH.exists():
            self.set_icon_from_file(str(ICON_PATH))

        self.store = Store()
        self.store.cleanup_orphaned_uploads()
        self.bridge = CodexBridge(
            dispatch=dispatch,
            on_event=self.on_codex_event,
            on_server_request=self.on_server_request,
            on_log=self.on_codex_log,
        )
        self.models: Dict[str, Dict[str, Any]] = {}
        self._effort_options_by_id: Dict[str, Dict[str, Any]] = {}
        self.active_room: Optional[Dict[str, Any]] = None
        self.loaded_threads: set[str] = set()
        self.pending_images: List[Dict[str, str]] = []
        # Singular aliases stay in sync for old helpers/tests and old drafts.
        self.pending_image: Optional[str] = None
        self.pending_image_label: Optional[str] = None
        self.pending_reply: Optional[Dict[str, Any]] = None
        self.current_theme = self.store.get_setting("theme", "night") or "night"
        self.current_accent = self.store.get_setting("accent", "blue") or "blue"
        self._syncing_appearance = False
        self.room_turns: Dict[int, Dict[str, Any]] = {}
        self._thread_token_totals: Dict[str, int] = {}
        self.message_bubbles: Dict[int, MessageBubble] = {}
        self.branch_controller = BranchController(
            self.store,
            lambda: self.bridge,
            self.loaded_threads,
            self.show_error,
        )
        self.room_buttons: Dict[int, Any] = {}
        self.room_titles: Dict[int, str] = {}
        self.unread_rooms: set[int] = self.store.list_unread_room_ids()
        self._loading_room = False
        self._syncing_composer_direction = False
        self._composer_direction_jobs: List[Dict[str, Any]] = []
        self._composer_direction_idle_source_id: Optional[int] = None
        self._composer_user_char_count = 0
        self._composer_non_whitespace_count = 0
        self._composer_generated_marker_count = 0
        self._composer_active_line_rtl = True
        self._composer_undo_stack: List[tuple[str, int, int]] = []
        self._composer_redo_stack: List[tuple[str, int, int]] = []
        self._composer_history_before: Optional[tuple[str, int, int]] = None
        self._composer_history_depth = 0
        self._composer_history_limit = 100
        self._restoring_composer_history = False
        self._syncing_controls = False
        self._draft_save_source_id: Optional[int] = None
        self._enter_send_armed_until = 0.0
        self._enter_send_hint_source_id: Optional[int] = None
        self._composer_notice_source_id: Optional[int] = None
        self._manual_scroll_serial = 0
        self._error_hide_source_id: Optional[int] = None
        self._sidebar_visible = (
            self.store.get_setting("sidebar_visible", "1") != "0"
        )
        self._oldest_message_id: Optional[int] = None
        self._load_older_button: Optional[Gtk.Button] = None
        self._room_load_generation = 0
        self._loading_older_messages = False
        self._last_history_scroll_serial = -1
        self._closing = False
        self._close_dialog: Optional[Gtk.Dialog] = None
        self._reconnect_attempts = 0
        self._reconnect_source_id: Optional[int] = None
        self.connected = False

        self._load_css()
        self._build_ui()
        self.apply_theme(self.current_theme, persist=False)
        self.apply_accent(self.current_accent, persist=False)
        self._timer_source_id = GLib.timeout_add_seconds(1, self._update_turn_timers)
        self.connect("delete-event", self._on_close)
        self.connect("key-press-event", self.on_window_key)
        self.refresh_rooms(select_first=True)
        self.set_status("Connecting…", "connecting")
        self.bridge.start(self.on_bridge_ready, self.on_bridge_error)

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(APP_DIR / "style.css"))
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        self.header = header
        header.set_direction(Gtk.TextDirection.LTR)
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        header.set_subtitle("Local desktop assistant")
        self.set_titlebar(header)

        self.sidebar_toggle = icon_button(
            "sidebar-show-symbolic", "Show or hide chats"
        )
        self.sidebar_toggle.connect("clicked", self.toggle_sidebar)
        header.pack_start(self.sidebar_toggle)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_direction(Gtk.TextDirection.LTR)
        controls.get_style_context().add_class("header-controls")

        self.status_label = Gtk.Label(label="")
        self.status_label.set_tooltip_text("Codex status")
        self.status_label.get_accessible().set_name("Codex status")
        self.status_label.get_style_context().add_class("status-pill")
        controls.pack_start(self.status_label, False, False, 0)

        self.model_combo = Gtk.ComboBoxText()
        self.model_combo.set_direction(Gtk.TextDirection.LTR)
        self.model_combo.set_size_request(176, -1)
        self.model_combo.set_tooltip_text(
            "Select a model for the active room. Ctrl+Alt+Up/Down changes it."
        )
        self.model_combo.get_accessible().set_name("Model")
        self.model_combo.get_style_context().add_class("model-combo")
        self.model_combo.connect("changed", self.on_model_changed)
        self.model_combo.connect("key-press-event", self.on_window_key)
        controls.pack_start(self.model_combo, False, False, 0)

        self.effort_combo = Gtk.ComboBoxText()
        self.effort_combo.set_direction(Gtk.TextDirection.LTR)
        self.effort_combo.set_size_request(100, -1)
        self.effort_combo.set_tooltip_text(
            "Reasoning effort for the active room. Alt+Up/Down changes it."
        )
        self.effort_combo.get_accessible().set_name("Reasoning effort")
        self.effort_combo.get_style_context().add_class("effort-combo")
        self.effort_combo.connect("changed", self.on_effort_changed)
        self.effort_combo.connect("key-press-event", self.on_window_key)
        controls.pack_start(self.effort_combo, False, False, 0)

        self.appearance_button = Gtk.MenuButton()
        self.appearance_button.set_image(
            Gtk.Image.new_from_icon_name(
                "preferences-desktop-theme-symbolic", Gtk.IconSize.BUTTON
            )
        )
        self.appearance_button.set_relief(Gtk.ReliefStyle.NONE)
        self.appearance_button.set_tooltip_text("Appearance")
        self.appearance_button.get_accessible().set_name("Appearance")
        self.appearance_button.get_style_context().add_class("appearance-button")
        appearance = Gtk.Popover.new(self.appearance_button)
        appearance.get_style_context().add_class("appearance-popover")
        appearance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        appearance_box.set_direction(Gtk.TextDirection.LTR)
        appearance_box.set_margin_top(12)
        appearance_box.set_margin_bottom(12)
        appearance_box.set_margin_start(12)
        appearance_box.set_margin_end(12)
        appearance_title = Gtk.Label(label="Appearance")
        appearance_title.set_xalign(0.0)
        appearance_title.get_style_context().add_class("appearance-title")
        appearance_box.pack_start(appearance_title, False, False, 0)

        mode_title = Gtk.Label(label="Mode")
        mode_title.set_xalign(0.0)
        mode_title.get_style_context().add_class("appearance-section")
        appearance_box.pack_start(mode_title, False, False, 0)
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._theme_buttons: Dict[str, Gtk.ToggleButton] = {}
        theme_group: Optional[Gtk.RadioButton] = None
        for theme_id, label in (
            ("night", "Night"),
            ("light", "Light"),
            ("graphite", "Graphite"),
        ):
            option = Gtk.RadioButton.new_with_label_from_widget(theme_group, label)
            theme_group = option
            option.set_active(theme_id == self.current_theme)
            option.get_style_context().add_class("appearance-choice")
            option.connect("toggled", self.on_theme_option, theme_id)
            self._theme_buttons[theme_id] = option
            mode_row.pack_start(option, True, True, 0)
        appearance_box.pack_start(mode_row, False, False, 0)

        accent_title = Gtk.Label(label="Accent")
        accent_title.set_xalign(0.0)
        accent_title.get_style_context().add_class("appearance-section")
        appearance_box.pack_start(accent_title, False, False, 0)
        accent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._accent_buttons: Dict[str, Gtk.ToggleButton] = {}
        accent_group: Optional[Gtk.RadioButton] = None
        for accent_id, label in (
            ("blue", "Blue"),
            ("red", "Red"),
            ("green", "Green"),
        ):
            option = Gtk.RadioButton.new_with_label_from_widget(accent_group, label)
            accent_group = option
            option.set_active(accent_id == self.current_accent)
            option.get_style_context().add_class("accent-swatch")
            option.get_style_context().add_class(f"accent-{accent_id}-swatch")
            option.connect("toggled", self.on_accent_option, accent_id)
            self._accent_buttons[accent_id] = option
            accent_row.pack_start(option, True, True, 0)
        appearance_box.pack_start(accent_row, False, False, 0)
        appearance.add(appearance_box)
        appearance_box.show_all()
        self.appearance_button.set_popover(appearance)
        controls.pack_start(self.appearance_button, False, False, 0)

        self.shortcuts_button = icon_button(
            "help-browser-symbolic", "Keyboard shortcuts (Ctrl+/)"
        )
        self.shortcuts_button.connect("clicked", self.show_keyboard_shortcuts)
        controls.pack_start(self.shortcuts_button, False, False, 0)
        header.pack_end(controls)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.set_direction(Gtk.TextDirection.LTR)
        self.add(root)

        chat_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        chat_column.set_hexpand(True)
        root.pack_start(chat_column, True, True, 0)

        self.error_bar = Gtk.InfoBar()
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        self.error_bar.set_show_close_button(True)
        self.error_bar.connect("response", lambda *_args: self.error_bar.hide())
        self.error_label = Gtk.Label()
        self.error_label.set_line_wrap(True)
        self.error_label.set_xalign(0.0)
        self.error_label.set_direction(Gtk.TextDirection.LTR)
        self.error_bar.get_content_area().add(self.error_label)
        self.error_bar.set_no_show_all(True)
        chat_column.pack_start(self.error_bar, False, False, 0)

        self.message_scroll = Gtk.ScrolledWindow()
        self.message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.message_scroll.set_overlay_scrolling(True)
        self.message_scroll.set_hexpand(True)
        self.message_scroll.set_vexpand(True)
        self.message_scroll.get_style_context().add_class("message-scroll")
        self.message_scroll.connect("scroll-event", self.on_manual_scroll)
        self.message_scroll.connect("button-press-event", self.on_manual_scroll)
        adjustment = self.message_scroll.get_vadjustment()
        adjustment.connect("value-changed", self.on_scroll_position_changed)
        adjustment.connect("changed", self.on_scroll_position_changed)

        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        transcript_shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        transcript_shell.set_direction(Gtk.TextDirection.LTR)
        self.message_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.message_box.set_hexpand(True)
        self.message_box.set_size_request(-1, -1)
        self.message_box.set_margin_top(22)
        self.message_box.set_margin_bottom(22)
        self.message_box.set_margin_start(24)
        self.message_box.set_margin_end(24)
        transcript_shell.pack_start(self.message_box, True, True, 0)
        viewport.add(transcript_shell)
        self.message_scroll.add(viewport)
        self.message_scroll.connect("size-allocate", self.on_chat_size_allocate)

        message_overlay = Gtk.Overlay()
        message_overlay.add(self.message_scroll)
        self.jump_bottom_button = icon_button(
            "go-bottom-symbolic", "Jump to the latest message", "jump-bottom-button"
        )
        self.jump_bottom_button.set_halign(Gtk.Align.CENTER)
        self.jump_bottom_button.set_valign(Gtk.Align.END)
        self.jump_bottom_button.set_margin_bottom(14)
        self.jump_bottom_button.set_no_show_all(True)
        self.jump_bottom_button.connect(
            "clicked", lambda _button: self.scroll_to_bottom(force=True)
        )
        message_overlay.add_overlay(self.jump_bottom_button)
        chat_column.pack_start(message_overlay, True, True, 0)

        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.composer = composer
        composer.get_style_context().add_class("composer")
        composer.set_margin_start(18)
        composer.set_margin_end(18)
        composer.set_margin_top(10)
        composer.set_margin_bottom(16)

        self.reply_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.reply_bar.set_direction(Gtk.TextDirection.LTR)
        self.reply_bar.get_style_context().add_class("reply-preview")
        self.reply_name = Gtk.Label(label="")
        self.reply_name.set_line_wrap(True)
        self.reply_name.set_max_width_chars(72)
        self.reply_name.set_xalign(0.0)
        cancel_reply = Gtk.Button.new_with_label("×")
        cancel_reply.set_relief(Gtk.ReliefStyle.NONE)
        cancel_reply.get_style_context().add_class("flat-action")
        cancel_reply.connect("clicked", lambda _button: self.clear_reply())
        self.reply_bar.pack_start(self.reply_name, True, True, 0)
        self.reply_bar.pack_end(cancel_reply, False, False, 0)
        self.reply_bar.set_no_show_all(True)
        composer.pack_start(self.reply_bar, False, False, 0)

        self.attachment_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.attachment_bar.set_direction(Gtk.TextDirection.LTR)
        self.attachment_bar.get_style_context().add_class("attachment-strip")
        attachment_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        attachment_head.set_direction(Gtk.TextDirection.LTR)
        self.attachment_name = Gtk.Label(label="")
        self.attachment_name.set_xalign(0.0)
        self.attachment_name.get_style_context().add_class("attachment-summary")
        attachment_head.pack_start(self.attachment_name, True, True, 0)
        add_attachment = Gtk.Button.new_with_label("＋ Add")
        add_attachment.set_relief(Gtk.ReliefStyle.NONE)
        add_attachment.set_valign(Gtk.Align.CENTER)
        add_attachment.get_style_context().add_class("attachment-add-button")
        add_attachment.connect("clicked", self.choose_image)
        attachment_head.pack_end(add_attachment, False, False, 0)
        clear_attachments = icon_button(
            "edit-clear-all-symbolic", "Remove all images", "attachment-clear-button"
        )
        clear_attachments.set_valign(Gtk.Align.CENTER)
        clear_attachments.connect("clicked", lambda _button: self.clear_attachment())
        attachment_head.pack_end(clear_attachments, False, False, 0)
        self.attachment_bar.pack_start(attachment_head, False, False, 0)
        self.attachment_flow = Gtk.FlowBox()
        self.attachment_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.attachment_flow.set_homogeneous(False)
        self.attachment_flow.set_min_children_per_line(1)
        self.attachment_flow.set_max_children_per_line(5)
        self.attachment_flow.set_row_spacing(6)
        self.attachment_flow.set_column_spacing(6)
        self.attachment_flow.set_halign(Gtk.Align.START)
        self.attachment_bar.pack_start(self.attachment_flow, False, False, 0)
        self.attachment_bar.set_no_show_all(True)
        composer.pack_start(self.attachment_bar, False, False, 0)

        compose_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compose_row.set_direction(Gtk.TextDirection.LTR)

        self.send_button = icon_button(
            "mail-send-symbolic", "Send message (press Enter twice)", "send-button"
        )
        self.send_button.set_tooltip_text("Send message (press Enter twice)")
        self.send_button.set_valign(Gtk.Align.CENTER)
        self.send_button.get_style_context().add_class("send-button")
        self.send_button.connect("clicked", lambda _button: self.send_message())
        compose_row.pack_end(self.send_button, False, False, 0)

        self.stop_button = icon_button(
            "media-playback-stop-symbolic", "Stop this chat's response", "stop-button"
        )
        self.stop_button.set_tooltip_text("Stops only the active chat")
        self.stop_button.set_valign(Gtk.Align.CENTER)
        self.stop_button.get_style_context().add_class("stop-button")
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self.stop_generation)
        compose_row.pack_end(self.stop_button, False, False, 0)

        self.image_button = icon_button(
            "image-x-generic-symbolic", "Add an image or paste with Ctrl+V", "image-button"
        )
        self.image_button.set_tooltip_text(
            "Choose an image, or paste one directly from the clipboard"
        )
        self.image_button.set_valign(Gtk.Align.CENTER)
        self.image_button.get_style_context().add_class("image-button")
        self.image_button.connect("clicked", self.choose_image)
        compose_row.pack_start(self.image_button, False, False, 0)

        input_scroll = Gtk.ScrolledWindow()
        input_scroll.set_direction(Gtk.TextDirection.LTR)
        input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroll.set_min_content_height(88)
        input_scroll.set_max_content_height(220)
        input_scroll.set_hexpand(True)
        input_scroll.set_overlay_scrolling(True)

        self.input_overlay = Gtk.Overlay()
        self.input_overlay.set_direction(Gtk.TextDirection.LTR)
        self.input_view = Gtk.TextView()
        self.input_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input_view.set_accepts_tab(False)
        self.input_view.set_left_margin(14)
        self.input_view.set_right_margin(14)
        self.input_view.set_top_margin(14)
        self.input_view.set_bottom_margin(14)
        self.input_view.set_direction(Gtk.TextDirection.RTL)
        self.input_view.set_justification(Gtk.Justification.LEFT)
        self.input_view.connect("key-press-event", self.on_input_key)
        # TextView has its own Alt+Arrow bindings, so listen directly here as
        # well as on the window to make the header shortcuts reliable while
        # the user is writing.
        self.input_view.connect("key-press-event", self.on_window_key)
        self.input_view.connect("paste-clipboard", self.on_paste_clipboard)
        input_buffer = self.input_view.get_buffer()
        self._composer_rtl_tag = input_buffer.create_tag(
            None,
            direction=Gtk.TextDirection.RTL,
            justification=Gtk.Justification.LEFT,
        )
        self._composer_ltr_tag = input_buffer.create_tag(
            None,
            direction=Gtk.TextDirection.LTR,
            justification=Gtk.Justification.LEFT,
        )
        self._composer_direction_marker_tag = input_buffer.create_tag(
            None,
        )
        input_buffer.connect("insert-text", self._on_composer_insert_text)
        input_buffer.connect("delete-range", self._on_composer_delete_range)
        input_buffer.connect("changed", self.on_draft_changed)
        input_buffer.connect(
            "begin-user-action", self._on_composer_user_action_begin
        )
        input_buffer.connect(
            "end-user-action", self._on_composer_user_action_end
        )
        input_scroll.add(self.input_view)
        self.input_overlay.add(input_scroll)
        self.input_placeholder = Gtk.Label(label="Message Codex…")
        self.input_placeholder.set_halign(Gtk.Align.END)
        self.input_placeholder.set_valign(Gtk.Align.START)
        self.input_placeholder.set_margin_top(12)
        self.input_placeholder.set_margin_start(16)
        self.input_placeholder.set_margin_end(16)
        # The placeholder itself is English. Keeping its widget LTR makes
        # START/END map to physical left/right; an RTL child would mirror END
        # back to the left even while the actual TextView is right-aligned.
        self.input_placeholder.set_direction(Gtk.TextDirection.LTR)
        self.input_placeholder.get_style_context().add_class("input-placeholder")
        self.input_placeholder.set_no_show_all(True)
        self.input_overlay.add_overlay(self.input_placeholder)
        compose_row.pack_start(self.input_overlay, True, True, 0)
        composer.pack_start(compose_row, True, True, 0)

        hint_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint_row.set_direction(Gtk.TextDirection.LTR)
        self.composer_hint = Gtk.Label(label=COMPOSER_HINT)
        self.composer_hint.set_xalign(0.0)
        self.composer_hint.get_style_context().add_class("composer-hint")
        hint_row.pack_start(self.composer_hint, True, True, 0)
        self.character_count = Gtk.Label(label="")
        self.character_count.set_direction(Gtk.TextDirection.LTR)
        self.character_count.get_style_context().add_class("character-count")
        hint_row.pack_end(self.character_count, False, False, 0)
        composer.pack_start(hint_row, False, False, 0)

        composer_shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        composer_shell.set_direction(Gtk.TextDirection.LTR)
        composer_shell.set_margin_top(8)
        composer_shell.set_margin_bottom(14)
        composer.set_hexpand(True)
        composer.set_size_request(-1, -1)
        composer_shell.pack_start(composer, True, True, 0)
        self.composer_shell = composer_shell
        chat_column.pack_end(composer_shell, False, False, 0)

        # Dropped screenshots behave exactly like images selected from disk.
        composer.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
            Gdk.DragAction.COPY,
        )
        composer.connect("drag-data-received", self.on_image_drop)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.sidebar = sidebar
        sidebar.set_size_request(252, -1)
        # Descendant room buttons request horizontal expansion.  Explicitly
        # contain that request here; otherwise GtkBox gives the sidebar an
        # oversized slot and leaves a conspicuous empty gutter on both sides.
        sidebar.set_hexpand(False)
        sidebar.set_hexpand_set(True)
        sidebar.set_direction(Gtk.TextDirection.LTR)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.set_margin_start(0)
        sidebar.set_margin_end(0)

        sidebar_title = Gtk.Label(label="Chats")
        sidebar_title.set_xalign(0.0)
        sidebar_title.get_style_context().add_class("sidebar-title")
        sidebar_title.set_margin_top(18)
        sidebar_title.set_margin_start(16)
        sidebar_title.set_margin_end(16)
        sidebar.pack_start(sidebar_title, False, False, 0)

        self.new_room_button = Gtk.Button.new_with_label("＋ New chat")
        self.new_room_button.set_tooltip_text("Create a new chat (Ctrl+N)")
        self.new_room_button.get_style_context().add_class("new-room-button")
        self.new_room_button.set_margin_start(14)
        self.new_room_button.set_margin_end(14)
        self.new_room_button.connect("clicked", self.new_room)
        sidebar.pack_start(self.new_room_button, False, False, 0)

        self.room_search = Gtk.SearchEntry()
        self.room_search.set_placeholder_text("Search chats")
        self.room_search.set_direction(Gtk.TextDirection.LTR)
        self.room_search.set_margin_start(14)
        self.room_search.set_margin_end(14)
        self.room_search.connect("search-changed", lambda _entry: self.refresh_rooms())
        sidebar.pack_start(self.room_search, False, False, 0)

        room_scroll = Gtk.ScrolledWindow()
        room_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        room_scroll.set_vexpand(True)
        self.room_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.room_box.set_margin_start(10)
        self.room_box.set_margin_end(10)
        room_viewport = Gtk.Viewport()
        room_viewport.set_shadow_type(Gtk.ShadowType.NONE)
        room_viewport.add(self.room_box)
        room_scroll.add(room_viewport)
        sidebar.pack_start(room_scroll, True, True, 0)

        security = Gtk.Label(label="● Full file and command access")
        security.set_xalign(0.5)
        security.set_margin_bottom(14)
        security.get_style_context().add_class("security-note")
        sidebar.pack_end(security, False, False, 0)
        self.sidebar_revealer = Gtk.Revealer()
        self.sidebar_revealer.set_hexpand(False)
        self.sidebar_revealer.set_hexpand_set(True)
        self.sidebar_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.sidebar_revealer.set_transition_duration(180)
        self.sidebar_revealer.add(sidebar)
        self.sidebar_revealer.set_reveal_child(self._sidebar_visible)
        root.pack_start(self.sidebar_revealer, False, False, 0)
        root.reorder_child(self.sidebar_revealer, 0)

    def on_theme_option(self, button: Gtk.ToggleButton, theme: str) -> None:
        if button.get_active() and not self._syncing_appearance:
            self.apply_theme(theme)

    def on_accent_option(self, button: Gtk.ToggleButton, accent: str) -> None:
        if button.get_active() and not self._syncing_appearance:
            self.apply_accent(accent)

    def _sync_appearance_buttons(self, values: Dict[str, Gtk.ToggleButton], active: str) -> None:
        self._syncing_appearance = True
        try:
            for name, button in values.items():
                button.set_active(name == active)
                context = button.get_style_context()
                (context.add_class if name == active else context.remove_class)("active")
        finally:
            self._syncing_appearance = False

    def apply_theme(self, theme: str, persist: bool = True) -> None:
        if theme not in {"night", "light", "graphite"}:
            theme = "night"
        context = self.get_style_context()
        for name in ("theme-night", "theme-light", "theme-graphite"):
            context.remove_class(name)
        context.add_class(f"theme-{theme}")
        self.current_theme = theme
        # Syntax foregrounds come from GtkSource schemes rather than CSS.
        # Update already-rendered fenced blocks when the user changes theme;
        # newly mounted MarkdownViews also discover their themed ancestor.
        pending: List[Gtk.Widget] = [self]
        while pending:
            widget = pending.pop()
            if isinstance(widget, MarkdownView):
                widget.set_code_theme(theme)
                continue
            if isinstance(widget, Gtk.Container):
                pending.extend(widget.get_children())
        if hasattr(self, "_theme_buttons"):
            self._sync_appearance_buttons(self._theme_buttons, theme)
        if hasattr(self, "appearance_button"):
            self.appearance_button.set_tooltip_text(
                f"{theme.title()} · {self.current_accent.title()}"
            )
        if persist:
            self.store.set_setting("theme", theme)

    def apply_accent(self, accent: str, persist: bool = True) -> None:
        if accent not in {"blue", "red", "green"}:
            accent = "blue"
        context = self.get_style_context()
        for name in ("accent-blue", "accent-red", "accent-green"):
            context.remove_class(name)
        context.add_class(f"accent-{accent}")
        self.current_accent = accent
        if hasattr(self, "_accent_buttons"):
            self._sync_appearance_buttons(self._accent_buttons, accent)
        if hasattr(self, "appearance_button"):
            label = f"{self.current_theme.title()} · {accent.title()}"
            self.appearance_button.set_tooltip_text(label)
        if persist:
            self.store.set_setting("accent", accent)

    def toggle_sidebar(self, _button: Optional[Gtk.Button] = None) -> None:
        self._sidebar_visible = not self.sidebar_revealer.get_reveal_child()
        self.sidebar_revealer.set_reveal_child(self._sidebar_visible)
        self.store.set_setting("sidebar_visible", "1" if self._sidebar_visible else "0")

    def on_chat_size_allocate(
        self, _widget: Gtk.Widget, allocation: Gdk.Rectangle
    ) -> None:
        # Quantised gutters keep the transcript centred but prevent a 10–15 px
        # scrollbar/layout change from nudging the whole conversation sideways.
        outer_padding = max(0, ((allocation.width - 980) // 64) * 32)
        self.message_box.set_margin_start(22 + outer_padding)
        self.message_box.set_margin_end(22 + outer_padding)
        self.composer_shell.set_margin_start(outer_padding)
        self.composer_shell.set_margin_end(outer_padding)

    def use_suggestion(self, prompt: str) -> None:
        text_buffer = self.input_view.get_buffer()
        text_buffer.begin_user_action()
        try:
            text_buffer.set_text(prompt)
        finally:
            text_buffer.end_user_action()
        self.input_view.grab_focus()
        self.input_view.place_cursor_onscreen()

    def update_composer_state(self) -> None:
        if hasattr(self, "_composer_user_char_count"):
            text_length = max(0, int(self._composer_user_char_count))
            has_content = bool(
                getattr(self, "_composer_non_whitespace_count", 0)
            )
        else:
            text = self._input_text() if hasattr(self, "input_view") else ""
            text_length = len(text)
            has_content = bool(text.strip())
        if hasattr(self, "input_placeholder"):
            self.input_placeholder.set_visible(not bool(text_length))
        if hasattr(self, "character_count"):
            self.character_count.set_text(
                str(text_length) if text_length >= 1000 else ""
            )
        if hasattr(self, "send_button") and self.active_room:
            room_id = int(self.active_room["id"])
            has_content = bool(has_content or self.pending_image)
            self.send_button.set_sensitive(
                has_content
                and self.connected
                and bool(self.models)
                and (
                    room_id not in self.room_turns
                    or bool(self.room_turns[room_id].get("turn_id"))
                )
            )

    def on_scroll_position_changed(self, *_args: Any) -> None:
        if not hasattr(self, "jump_bottom_button"):
            return
        should_show = not self.is_near_bottom(110.0)
        self.jump_bottom_button.set_no_show_all(not should_show)
        if should_show:
            self.jump_bottom_button.show()
        else:
            self.jump_bottom_button.hide()
        adjustment = self.message_scroll.get_vadjustment()
        if (
            self._load_older_button is not None
            and not self._loading_older_messages
            and adjustment.get_value() <= 90.0
            and self._manual_scroll_serial != self._last_history_scroll_serial
        ):
            self._last_history_scroll_serial = self._manual_scroll_serial
            GLib.idle_add(self.load_older_messages, None)

    def start_reply(
        self, message_id: Optional[int], role: str, content: str
    ) -> None:
        if message_id is None:
            return
        preview = " ".join(content.strip().split())[:180] or "Image message"
        self.pending_reply = {
            "id": int(message_id),
            "role": role,
            "content": content,
            "preview": preview,
        }
        author = "You" if role == "user" else "Codex"
        self.reply_name.set_text(f"Replying to {author}: {preview}")
        self.reply_bar.set_no_show_all(False)
        self.reply_bar.show_all()
        self.input_view.grab_focus()
        self.save_active_draft()

    def clear_reply(self, persist: bool = True) -> None:
        self.pending_reply = None
        if hasattr(self, "reply_name"):
            self.reply_name.set_text("")
        if hasattr(self, "reply_bar"):
            self.reply_bar.hide()
            self.reply_bar.set_no_show_all(True)
        if persist:
            self.save_active_draft()

    def _mark_turn_reconnecting(
        self, state: Dict[str, Any], detail: str, source: str = "transport"
    ) -> None:
        if state.get("connection_lost_at") is None:
            state["connection_lost_at"] = time.monotonic()
        stopping = bool(state.get("stop_requested"))
        set_state_activity_presentation(
            state, "stopping" if stopping else "reconnecting"
        )
        record_state_activity(
            state,
            stage="Connection",
            title=(
                "Stop request is waiting for Codex to reconnect…"
                if stopping
                else "Connection lost; reconnecting…"
            ),
            detail=detail,
            status="warning",
            source=source,
            key="connection",
        )

    def _expire_disconnected_turns(self, now: Optional[float] = None) -> None:
        current_time = time.monotonic() if now is None else now
        expired_room_ids = [
            room_id
            for room_id, state in self.room_turns.items()
            if state.get("connection_lost_at") is not None
            and current_time - float(state["connection_lost_at"])
            >= DISCONNECTED_TURN_TIMEOUT_SECONDS
        ]
        for room_id in expired_room_ids:
            self.finish_turn(
                room_id,
                error_text=(
                    "Connection timed out before Codex finished the response. "
                    "Please try again."
                ),
            )
        if expired_room_ids:
            self.set_status("Connection unavailable", "error")

    def on_bridge_ready(self, _result: Dict[str, Any]) -> bool:
        if getattr(self, "_closing", False):
            return False
        self.connected = True
        self._reconnect_attempts = 0
        self._reconnect_source_id = None
        self.set_status("Connected", "online")
        for state in self.room_turns.values():
            reconnecting = str(state.get("activity_presentation") or "") == "reconnecting"
            if state.pop("connection_lost_at", None) is not None or reconnecting:
                if state.get("stop_requested"):
                    set_state_activity_presentation(state, "stopping")
                    if state.get("thread_id") and state.get("turn_id"):
                        self.bridge.interrupt(state["thread_id"], state["turn_id"])
                else:
                    set_state_activity_presentation(state, "restoring")
                    ingest_state_activity(state, "bridge/reconnected", {})
        self.sync_active_room_controls()
        self.refresh_rooms()
        self.bridge.list_models(
            self.on_models_loaded,
            self.on_models_error,
        )
        return False

    def on_models_error(self, error: Dict[str, Any]) -> bool:
        if error.get("transient"):
            return False
        self.show_error("Could not load models", str(error.get("message") or error))
        return False

    def on_bridge_error(self, error: Dict[str, Any]) -> bool:
        if self._closing:
            return False
        self.connected = False
        message = error.get("message") or str(error)
        self.set_status(
            "Reconnecting…" if error.get("transient") else "Connection error",
            "connecting" if error.get("transient") else "error",
        )
        self.sync_active_room_controls()
        detail = (
            "The response is waiting for Codex to reconnect."
            if error.get("transient")
            else "Reconnect attempt failed; retrying."
        )
        for state in self.room_turns.values():
            self._mark_turn_reconnecting(state, detail)
        self.refresh_rooms()
        if not error.get("transient"):
            self.show_error("Could not connect to Codex", message)
        self.schedule_reconnect()
        return False

    def schedule_reconnect(self) -> None:
        if self._closing or self._reconnect_source_id is not None:
            return
        delay = min(8, 1 + self._reconnect_attempts * 2)

        def reconnect() -> bool:
            self._reconnect_source_id = None
            if self._closing or self.connected:
                return False
            self._reconnect_attempts += 1
            self.set_status(f"Reconnecting • {self._reconnect_attempts}", "connecting")
            self.bridge.close()
            self.bridge = CodexBridge(
                dispatch=dispatch,
                on_event=self.on_codex_event,
                on_server_request=self.on_server_request,
                on_log=self.on_codex_log,
            )
            self.bridge.start(self.on_bridge_ready, self.on_bridge_error)
            return False

        self._reconnect_source_id = GLib.timeout_add_seconds(delay, reconnect)

    def on_codex_log(self, line: str) -> bool:
        if "WARNING" not in line:
            print(line, file=sys.stderr)
        return False

    def on_models_loaded(self, result: Dict[str, Any]) -> bool:
        items = result.get("data") or []
        self.models = {
            str(item.get("id") or item.get("model")): item
            for item in items
            if item.get("id") or item.get("model")
        }
        requested = (
            str(self.active_room.get("model") or "") if self.active_room else None
        )
        preferred = preferred_model_id(self.models, requested)
        self._syncing_controls = True
        try:
            self.model_combo.remove_all()
            for model_id, item in self.models.items():
                self.model_combo.append(model_id, model_display_label(model_id, item))
            if preferred:
                self.model_combo.set_active_id(preferred)
            self.update_effort_options()
        finally:
            self._syncing_controls = False
        self.update_model_tooltip()
        self.sync_active_room_controls()
        if not self.models:
            self.set_status("No models found", "warning")
        return False

    def update_effort_options(self) -> None:
        model_id = self.model_combo.get_active_id()
        model = self.models.get(model_id or "", {})
        options = ordered_effort_options(model, model_id=model_id)
        self._effort_options_by_id = {str(option["id"]): option for option in options}
        current = str(self.active_room.get("effort") or "") if self.active_room else ""
        supported_ids = [str(option["id"]) for option in options]
        default = str(model.get("defaultReasoningEffort") or "").strip().lower()
        choice = current if current in supported_ids else (
            default if default in supported_ids else None
        )
        # Gtk emits ``changed`` while a ComboBoxText is rebuilt.  Treat that as
        # control synchronization, never as a user edit to the active room.
        was_syncing = self._syncing_controls
        self._syncing_controls = True
        try:
            self.effort_combo.remove_all()
            for option in options:
                self.effort_combo.append(str(option["id"]), str(option["label"]))
            if choice:
                self.effort_combo.set_active_id(choice)
            elif options:
                self.effort_combo.set_active(0)
        finally:
            self._syncing_controls = was_syncing
        self.update_effort_tooltip()
        self.effort_combo.set_sensitive(
            bool(options)
            and self.active_room is not None
            and not room_is_busy(self.active_room, self.room_turns)
        )

    def update_model_tooltip(self) -> None:
        model_id = self.model_combo.get_active_id()
        model = self.models.get(model_id or "")
        self.model_combo.set_tooltip_text(
            model_tooltip_text(str(model_id), model)
            if model_id and model
            else "No model is available."
        )

    def update_effort_tooltip(self) -> None:
        effort = self.effort_combo.get_active_id()
        option = self._effort_options_by_id.get(str(effort or ""))
        if not option:
            self.effort_combo.set_tooltip_text(
                "Reasoning effort is not available for this model."
            )
            return
        default_note = " Default for this model." if option.get("is_default") else ""
        self.effort_combo.set_tooltip_text(
            f"{option['base_label']} reasoning effort.{default_note}\n"
            f"{option['description']}"
        )

    def on_model_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self.update_model_tooltip()
        if not self.active_room or self._loading_room or self._syncing_controls:
            return
        if room_is_busy(self.active_room, self.room_turns):
            return
        model = self.model_combo.get_active_id()
        if model:
            self.store.update_room(int(self.active_room["id"]), model=model)
            self.active_room["model"] = model
        self.update_effort_options()
        effort = self.effort_combo.get_active_id()
        if effort and effort != self.active_room.get("effort"):
            self.store.update_room(int(self.active_room["id"]), effort=effort)
            self.active_room["effort"] = effort
        elif not effort and self.active_room.get("effort") is not None:
            # A model may expose no reasoning controls. Do not leave an old
            # unsupported level in the room record, where it could reappear
            # after a reload or be sent as an invalid request.
            self.store.update_room(int(self.active_room["id"]), effort=None)
            self.active_room["effort"] = None

    def on_effort_changed(self, _combo: Gtk.ComboBoxText) -> None:
        self.update_effort_tooltip()
        if not self.active_room or self._loading_room or self._syncing_controls:
            return
        if room_is_busy(self.active_room, self.room_turns):
            return
        effort = self.effort_combo.get_active_id()
        if effort:
            self.store.update_room(int(self.active_room["id"]), effort=effort)
            self.active_room["effort"] = effort

    def refresh_rooms(self, select_first: bool = False) -> None:
        for child in self.room_box.get_children():
            self.room_box.remove(child)
        query = self.room_search.get_text() if hasattr(self, "room_search") else ""
        rooms = self.store.list_room_summaries(query)
        self.room_buttons.clear()
        self.room_titles.clear()
        if not rooms and not query and not self.store.list_rooms():
            rooms = [self.store.create_room()]

        active_id = int(self.active_room["id"]) if self.active_room else None
        for room in rooms:
            room_id = int(room["id"])
            state = self.room_turns.get(room_id)
            preview = " ".join(str(room.get("last_message") or "New chat").split())
            if len(preview) > 54:
                preview = preview[:53] + "…"
            kind = ""
            if state:
                presentation = str(state.get("activity_presentation") or "working")
                if state.get("stop_requested") or presentation == "stopping":
                    preview = "● Stopping"
                    kind = "stopping-room"
                elif presentation == "reconnecting":
                    preview = "● Reconnecting"
                    kind = "reconnecting-room"
                elif presentation == "restoring":
                    preview = "● Restoring"
                    kind = "reconnecting-room"
                else:
                    preview = "● Responding"
                    kind = "busy-room"
            elif room_id in self.unread_rooms:
                preview = "● New response — " + preview
                kind = "unread-room"
            row = RoomRow(
                room_id,
                str(room["title"]),
                preview,
                self.select_room,
                self.rename_room,
                self.export_room,
                self.delete_room,
            )
            row.set_kind(kind)
            row.set_selected(active_id == room_id)
            self.room_buttons[room_id] = row
            self.room_titles[room_id] = str(room["title"])
            self.room_box.pack_start(row, False, False, 0)
        if not rooms and query:
            empty = Gtk.Label(label="No chats found")
            empty.get_style_context().add_class("sidebar-empty")
            empty.set_margin_top(24)
            self.room_box.pack_start(empty, False, False, 0)
        self.room_box.show_all()

        if select_first and not self.active_room and rooms:
            self.load_room(rooms[0])

    def new_room(self, _button: Gtk.Button) -> None:
        if hasattr(self, "room_search"):
            self.room_search.set_text("")
        room = self.store.create_room()
        self.load_room(room)
        self.refresh_rooms()
        self.input_view.grab_focus()

    def rename_room(self, room_id: int) -> None:
        room = self.store.get_room(room_id)
        if not room:
            return
        dialog = Gtk.Dialog(
            title="Rename chat",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Chat name")
        entry.set_text(str(room["title"]))
        entry.select_region(0, -1)
        entry.set_activates_default(True)
        entry.set_direction(Gtk.TextDirection.LTR)
        dialog.set_default_response(Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        area.add(entry)
        dialog.show_all()
        response = dialog.run()
        title = entry.get_text()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and title.strip():
            renamed = self.store.rename_room(room_id, title)
            if renamed and self.active_room and int(self.active_room["id"]) == room_id:
                self.active_room = renamed
                self.header.set_title(str(renamed["title"]))
            self.refresh_rooms()
            if renamed and renamed.get("thread_id") and self.connected:
                self.bridge.set_thread_name(
                    str(renamed["thread_id"]),
                    str(renamed["title"]),
                    errback=lambda error: self.show_error(
                        "Local name saved",
                        "Could not sync the name to Codex: " + str(error.get("message") or error),
                    ),
                )

    def delete_room(self, room_id: int) -> None:
        room = self.store.get_room(room_id)
        if not room:
            return
        if room_id in self.room_turns:
            self.show_error(
                "Chat is busy",
                "Stop this chat's response before deleting it.",
            )
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Delete this chat?",
        )
        remote = bool(room.get("thread_id") and self.connected)
        detail = (
            "The local history and matching Codex thread will be permanently deleted."
            if remote
            else "Only this app's local history will be deleted."
        )
        dialog.format_secondary_text(f"{room['title']} — {detail}")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Delete", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        def delete_local() -> None:
            deleting_active = bool(
                self.active_room and int(self.active_room["id"]) == room_id
            )
            if deleting_active:
                self.save_active_draft()
                self.active_room = None
            self.store.delete_room(room_id)
            rooms = self.store.list_rooms()
            if not rooms:
                rooms = [self.store.create_room()]
            if deleting_active:
                self.load_room(rooms[0])
            self.refresh_rooms()

        if remote:
            self.bridge.delete_thread(
                str(room["thread_id"]),
                lambda _result: delete_local(),
                lambda error: self.show_error(
                    "Could not delete chat", str(error.get("message") or error)
                ),
            )
            return
        delete_local()

    def export_room(self, room_id: int) -> None:
        room = self.store.get_room(room_id)
        if not room:
            return
        safe_title = re.sub(r"[^\w\- ]+", "", str(room["title"]), flags=re.UNICODE).strip()
        dialog = Gtk.FileChooserDialog(
            title="Export chat",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(f"{safe_title or 'conversation'}.md")
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not filename:
            return
        try:
            Path(filename).write_text(
                conversation_markdown(room, self.store.list_messages(room_id)),
                encoding="utf-8",
            )
        except OSError as exc:
            self.show_error("Could not save export", str(exc))

    def select_room(self, room_id: int) -> None:
        if self.active_room and int(self.active_room["id"]) == int(room_id):
            self.input_view.grab_focus()
            return
        room = self.store.get_room(room_id)
        if room:
            self.load_room(room)

    def show_keyboard_shortcuts(self, _button: Optional[Gtk.Button] = None) -> None:
        dialog = Gtk.Dialog(
            title="Keyboard shortcuts",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=28, row_spacing=12)
        grid.set_margin_top(18)
        grid.set_margin_bottom(18)
        grid.set_margin_start(20)
        grid.set_margin_end(20)
        shortcuts = (
            ("New chat", "Ctrl+N"),
            ("Focus composer", "Ctrl+L"),
            ("Search chats", "Ctrl+K"),
            ("Show or hide chats", "Ctrl+B"),
            ("Next / previous chat", "Ctrl+Tab / Ctrl+Shift+Tab"),
            ("Copy latest response", "Ctrl+Shift+C"),
            ("Export current chat", "Ctrl+Shift+S"),
            ("Undo / redo composer edit", "Ctrl+Z / Ctrl+Shift+Z or Ctrl+Y"),
            ("Previous / next reasoning effort", "Alt+Down / Alt+Up"),
            ("Previous / next model", "Ctrl+Alt+Down / Ctrl+Alt+Up"),
            ("Show shortcuts", "Ctrl+/"),
        )
        for row, (action, shortcut) in enumerate(shortcuts):
            action_label = Gtk.Label(label=action)
            action_label.set_xalign(0.0)
            shortcut_label = Gtk.Label(label=shortcut)
            shortcut_label.set_xalign(1.0)
            shortcut_label.get_style_context().add_class("dim-label")
            grid.attach(action_label, 0, row, 1, 1)
            grid.attach(shortcut_label, 1, row, 1, 1)
        dialog.get_content_area().add(grid)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _show_composer_notice(self, text: str) -> None:
        if self._composer_notice_source_id is not None:
            GLib.source_remove(self._composer_notice_source_id)
        self.composer_hint.set_text(text)

        def restore_hint() -> bool:
            self._composer_notice_source_id = None
            if self.composer_hint.get_text() == text:
                self.composer_hint.set_text(COMPOSER_HINT)
            return False

        self._composer_notice_source_id = GLib.timeout_add(1800, restore_hint)

    def copy_latest_response(self) -> bool:
        if not self.active_room:
            return False
        messages = self.store.list_messages(int(self.active_room["id"]))
        response = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "assistant"
                and str(message.get("content") or "").strip()
            ),
            "",
        )
        if not response:
            self._show_composer_notice("No response to copy yet")
            return False
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(response, -1)
        clipboard.store()
        self._show_composer_notice("Latest response copied")
        return True

    def _can_cycle_header_selector(self) -> bool:
        """Whether a keyboard shortcut may edit the active room's settings."""
        return bool(
            self.active_room
            and self.models
            and not self._loading_room
            and not self._syncing_controls
            and not room_is_busy(self.active_room, self.room_turns)
        )

    def _set_header_combo_from_shortcut(
        self,
        combo: Gtk.ComboBoxText,
        value: str,
        callback: Any,
    ) -> bool:
        """Select one value and persist it once, even with a headless combo.

        GTK emits ``changed`` synchronously from ``set_active_id``.  Suppress
        that intermediate signal, then invoke the normal controller callback
        once after the selection is stable.  This gives shortcuts the exact
        same persistence and model-to-effort clamping path as a mouse click.
        """
        if str(combo.get_active_id() or "") == value:
            return False
        was_syncing = self._syncing_controls
        self._syncing_controls = True
        try:
            combo.set_active_id(value)
        finally:
            self._syncing_controls = was_syncing
        if str(combo.get_active_id() or "") != value:
            return False
        callback(combo)
        return True

    def _cycle_effort_from_shortcut(self, direction: int) -> bool:
        if not self._can_cycle_header_selector():
            return False
        model_id = str(self.model_combo.get_active_id() or "")
        options = ordered_effort_options(
            self.models.get(model_id, {}), model_id=model_id
        )
        effort_ids = [str(option["id"]) for option in options]
        if not effort_ids:
            return False
        current = str(self.effort_combo.get_active_id() or "")
        if current in effort_ids:
            index = effort_ids.index(current)
            target = effort_ids[max(0, min(len(effort_ids) - 1, index + direction))]
        else:
            target = effort_ids[0] if direction > 0 else effort_ids[-1]
        return self._set_header_combo_from_shortcut(
            self.effort_combo, target, self.on_effort_changed
        )

    def _cycle_model_from_shortcut(self, direction: int) -> bool:
        if not self._can_cycle_header_selector():
            return False
        model_ids = list(self.models)
        if not model_ids:
            return False
        current = str(self.model_combo.get_active_id() or "")
        if current in model_ids:
            index = model_ids.index(current)
            # Models deliberately cycle; unlike reasoning effort they do not
            # form a bounded numeric scale.
            target = model_ids[(index + direction) % len(model_ids)]
        else:
            target = model_ids[0] if direction > 0 else model_ids[-1]
        return self._set_header_combo_from_shortcut(
            self.model_combo, target, self.on_model_changed
        )

    def on_window_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if self.pending_reply:
                self.clear_reply()
                return True
            if self.pending_image:
                self.clear_attachment()
                return True
            if self.error_bar.get_visible():
                self.error_bar.hide()
                return True
            for bubble in self.message_bubbles.values():
                if bubble.selected_text:
                    bubble.content_view.clear_selection()
                    return True
        # Keep these above the Ctrl-only command block so Alt+Arrow works
        # from the composer as well as from either header selector.
        if event.keyval in (
            Gdk.KEY_Up,
            Gdk.KEY_Down,
            Gdk.KEY_KP_Up,
            Gdk.KEY_KP_Down,
        ) and event.state & Gdk.ModifierType.MOD1_MASK:
            # Shift+Alt+Arrow and Super+Alt+Arrow keep their usual desktop or
            # widget meaning.  NumLock/CapsLock are intentionally harmless.
            disallowed = (
                Gdk.ModifierType.SHIFT_MASK
                | Gdk.ModifierType.SUPER_MASK
                | Gdk.ModifierType.META_MASK
                | Gdk.ModifierType.HYPER_MASK
            )
            if not event.state & disallowed:
                direction = (
                    1
                    if event.keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up)
                    else -1
                )
                if event.state & Gdk.ModifierType.CONTROL_MASK:
                    # Models are an ordered list rather than an effort scale:
                    # Up moves to the previous visible row; Down to the next.
                    self._cycle_model_from_shortcut(-direction)
                else:
                    self._cycle_effort_from_shortcut(direction)
                return True
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        keyval = event.keyval
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        translated = keymap.translate_keyboard_state(
            event.hardware_keycode, Gdk.ModifierType(0), 0
        )
        if translated[0]:
            keyval = translated[1]
        if keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self.new_room(self.new_room_button)
            return True
        if keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.input_view.grab_focus()
            return True
        if keyval in (Gdk.KEY_k, Gdk.KEY_K):
            if not self.sidebar_revealer.get_reveal_child():
                self.toggle_sidebar()
            self.room_search.grab_focus()
            return True
        if keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self.toggle_sidebar()
            return True
        if keyval == Gdk.KEY_slash:
            self.show_keyboard_shortcuts()
            return True
        if (
            event.state & Gdk.ModifierType.SHIFT_MASK
            and keyval in (Gdk.KEY_c, Gdk.KEY_C)
        ):
            self.copy_latest_response()
            return True
        if (
            event.state & Gdk.ModifierType.SHIFT_MASK
            and keyval in (Gdk.KEY_s, Gdk.KEY_S)
        ):
            if self.active_room:
                self.export_room(int(self.active_room["id"]))
            return True
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            rooms = self.store.list_rooms()
            if len(rooms) < 2 or not self.active_room:
                return True
            ids = [int(room["id"]) for room in rooms]
            current = int(self.active_room["id"])
            index = ids.index(current) if current in ids else 0
            direction = -1 if event.state & Gdk.ModifierType.SHIFT_MASK else 1
            room = self.store.get_room(ids[(index + direction) % len(ids)])
            if room:
                self.load_room(room)
            return True
        return False

    def load_room(self, room: Dict[str, Any]) -> None:
        self._room_load_generation += 1
        generation = self._room_load_generation
        self._loading_older_messages = False
        self._last_history_scroll_serial = self._manual_scroll_serial
        self.save_active_draft()
        self.save_active_view_state()
        if self.active_room:
            previous = self.room_turns.get(int(self.active_room["id"]))
            if previous:
                previous["bubble"] = None
        self.active_room = room
        room_id = int(room["id"])
        self.header.set_title(str(room.get("title") or APP_NAME))
        self.header.set_subtitle("Local desktop assistant")
        self.unread_rooms.discard(room_id)
        self.store.mark_room_read(room_id)
        self._loading_room = True
        self.clear_attachment(persist=False)
        self.clear_reply(persist=False)
        self._clear_composer_direction_jobs()
        self.input_view.get_buffer().set_text(str(room.get("draft_text") or ""))
        self._reset_composer_history()
        draft_paths = list(room.get("draft_image_paths") or [])
        if not draft_paths and room.get("draft_image_path"):
            draft_paths = [str(room["draft_image_path"])]
        draft_labels = list(room.get("draft_image_labels") or [])
        valid_paths: List[str] = []
        valid_labels: List[str] = []
        stale_paths: List[str] = []
        for index, draft_image in enumerate(draft_paths):
            path = str(draft_image)
            label = (
                str(draft_labels[index])
                if index < len(draft_labels)
                else Path(path).name
            )
            try:
                if not Path(path).exists():
                    raise OSError("The draft attachment no longer exists")
                self.set_attachment(path, label, persist=False)
                valid_paths.append(path)
                valid_labels.append(label)
            except (GLib.Error, OSError, ValueError):
                stale_paths.append(path)
        if stale_paths:
            self.store.update_room(
                room_id,
                draft_image_paths=valid_paths,
                draft_image_labels=valid_labels,
            )
            room["draft_image_paths"] = valid_paths
            room["draft_image_labels"] = valid_labels
            for stale_path in stale_paths:
                self.store.remove_image_if_unused(stale_path)
        draft_reply_id = room.get("draft_reply_to_id")
        if draft_reply_id:
            target = self.store.get_message(int(draft_reply_id))
            if target and int(target["room_id"]) == room_id:
                self.start_reply(
                    int(target["id"]),
                    str(target["role"]),
                    str(room.get("draft_reply_quote") or target["content"]),
                )
        self._loading_room = False
        for old_bubble in self.message_bubbles.values():
            for branch_id, panel in old_bubble.branch_panels.items():
                self.branch_controller.unregister_panel(branch_id, panel)
        self.message_bubbles.clear()
        for child in self.message_box.get_children():
            self.message_box.remove(child)
        # A room switch must be about its newest messages.  Do not fetch a
        # bigger page and prepend it later: that visibly moves the transcript
        # while the user is already reading the bottom of the conversation.
        messages = self.store.list_messages_with_replies(
            room_id, limit=ROOM_INITIAL_RENDER_COUNT
        )
        self._oldest_message_id = int(messages[0]["id"]) if messages else None
        self._load_older_button = None
        has_older = bool(
            self._oldest_message_id
            and self.store.has_messages_before(room_id, self._oldest_message_id)
        )
        if not messages:
            self.message_box.pack_start(
                EmptyState(self.use_suggestion), True, True, 0
            )
        else:
            for message in messages:
                bubble = self.make_message_bubble(message)
                self.message_box.pack_start(bubble, False, False, 0)
        state = self.room_turns.get(room_id)
        if state:
            bubble = MessageBubble(
                "assistant",
                inflight_turn_display_text(state),
                streaming=True,
                on_reply=self.start_reply,
                on_branch=self.open_side_thread,
            )
            state["bubble"] = bubble
            bubble.set_elapsed(
                int(time.monotonic() - state["started_at"]),
                bool(state.get("stop_requested")),
            )
            if state.get("activity_snapshot"):
                present_activity_snapshot(state, state["activity_snapshot"])
            elif state.get("activity_text"):
                bubble.set_activity(str(state["activity_text"]))
                set_state_activity_presentation(
                    state,
                    str(state.get("activity_presentation") or "working"),
                )
            self.message_box.pack_start(bubble, False, False, 0)
        self.message_box.show_all()
        if has_older:
            self._add_load_older_button()
            self.message_box.reorder_child(self._load_older_button, 0)
            self.message_box.show_all()

        if self.models:
            model = preferred_model_id(self.models, room.get("model"))
            self._syncing_controls = True
            try:
                if model:
                    self.model_combo.set_active_id(str(model))
                self.update_effort_options()
            finally:
                self._syncing_controls = False
            self.update_model_tooltip()
        self.refresh_rooms()
        self.sync_active_room_controls()
        self.update_composer_state()
        self.scroll_to_bottom(generation=generation)

    def make_message_bubble(self, message: Dict[str, Any]) -> MessageBubble:
        reply_preview = None
        if message.get("reply_quote"):
            reply_preview = " ".join(str(message["reply_quote"]).split())[:180]
        elif message.get("reply_content") is not None:
            reply_preview = " ".join(str(message["reply_content"]).split())[:180]
            if not reply_preview and (
                message.get("reply_image_paths") or message.get("reply_image_path")
            ):
                reply_preview = "Image message"
        elif message.get("reply_to_id"):
            target = self.store.get_message(int(message["reply_to_id"]))
            if target:
                reply_preview = " ".join(str(target["content"]).split())[:180]
        content = str(message.get("content") or "")
        display_content = content or (
            "Attached image"
            if (message.get("image_paths") or message.get("image_path"))
            else ""
        )
        bubble = MessageBubble(
            str(message["role"]),
            display_content,
            message.get("image_path"),
            message_id=int(message["id"]),
            reply_preview=reply_preview,
            duration_seconds=message.get("duration_seconds"),
            on_reply=self.start_reply,
            created_at=message.get("created_at"),
            token_usage=message.get("token_usage"),
            on_branch=self.open_side_thread,
            image_paths=list(message.get("image_paths") or []),
        )
        message_id = int(message["id"])
        self.message_bubbles[message_id] = bubble
        if str(message["role"]) == "assistant":
            for branch in self.store.list_branches(
                int(message["room_id"]), message_id
            ):
                self._mount_branch_panel(branch, bubble)
        return bubble

    def _mount_branch_panel(
        self, branch: Dict[str, Any], bubble: MessageBubble
    ) -> BranchPanel:
        branch_id = int(branch["id"])
        existing = bubble.branch_panels.get(branch_id)
        if existing is not None:
            return existing
        panel = BranchPanel(
            branch_id,
            str(branch.get("selected_quote") or "Selected passage"),
            self.store.list_branch_messages(branch_id),
            self.send_branch_question,
            on_close=lambda bid, item, owner=bubble: owner.collapse_branch(
                bid, item
            ),
        )
        bubble.add_branch_panel(panel)
        self.branch_controller.register_panel(branch_id, panel)
        return panel

    def open_side_thread(
        self, message_id: Optional[int], selected_quote: str, bubble: MessageBubble
    ) -> None:
        if not self.active_room or message_id is None or not selected_quote.strip():
            return
        room_id = int(self.active_room["id"])
        try:
            branch = self.store.create_branch(
                room_id, int(message_id), selected_quote
            )
        except (ValueError, OSError) as exc:
            self.show_error("Could not create side thread", str(exc))
            return
        panel = self._mount_branch_panel(branch, bubble)
        panel.show_all()
        panel.entry.grab_focus()
        self.scroll_to_bottom(force=False)

    def send_branch_question(
        self, branch_id: int, question: str, panel: BranchPanel
    ) -> None:
        if not self.connected:
            self.show_error(
                "Connection unavailable",
                "Reconnect before sending a side-thread question.",
            )
            return
        branch = self.store.get_branch(int(branch_id))
        if not branch:
            self.show_error("Side thread unavailable", "This side thread no longer exists.")
            return
        room = self.store.get_room(int(branch["room_id"]))
        if not room or not self.models:
            self.show_error("Model unavailable", "Wait for the model list to load.")
            return
        model = preferred_model_id(self.models, room.get("model"))
        if not model:
            self.show_error("Model unavailable", "No compatible model was found.")
            return
        options = ordered_effort_options(self.models[model], model_id=model)
        supported = {str(option["id"]) for option in options}
        effort = str(room.get("effort") or "")
        if effort not in supported:
            effort = next(
                (str(option["id"]) for option in options if option["is_default"]),
                str(options[0]["id"]) if options else "",
            )
        # Persist the resolved defaults before the request.  If the window is
        # closed while the broker keeps this branch turn alive, replay can
        # reconstruct the exact model/effort choice from the room.
        self.store.update_room(
            int(branch["room_id"]), model=str(model), effort=effort or None
        )
        self.branch_controller.ask(
            int(branch_id),
            question,
            panel,
            str(model),
            effort or None,
            DEFAULT_CWD,
        )

    def _add_load_older_button(self) -> None:
        if self._load_older_button is not None:
            return
        button = Gtk.Button.new_with_label("Load older messages")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("load-older-button")
        button.connect("clicked", self.load_older_messages)
        self._load_older_button = button
        self.message_box.pack_start(button, False, False, 0)

    def load_older_messages(self, _button: Optional[Gtk.Button]) -> bool:
        if (
            not self.active_room
            or self._oldest_message_id is None
            or self._loading_older_messages
        ):
            return False
        self._loading_older_messages = True
        room_id = int(self.active_room["id"])
        generation = self._room_load_generation
        scroll_serial = self._manual_scroll_serial
        adjustment = self.message_scroll.get_vadjustment()
        old_upper = adjustment.get_upper()
        old_value = adjustment.get_value()
        older = self.store.list_messages_with_replies(
            room_id, limit=ROOM_OLDER_PAGE_SIZE, before_id=self._oldest_message_id
        )
        if not older:
            self._loading_older_messages = False
            return False
        if self._load_older_button:
            self.message_box.remove(self._load_older_button)
            self._load_older_button = None
        widgets = [self.make_message_bubble(message) for message in older]
        for widget in widgets:
            self.message_box.pack_start(widget, False, False, 0)
        self._oldest_message_id = int(older[0]["id"])
        offset = 0
        if self.store.has_messages_before(room_id, self._oldest_message_id):
            self._add_load_older_button()
            self.message_box.reorder_child(self._load_older_button, 0)
            offset = 1
        for index, widget in enumerate(widgets):
            self.message_box.reorder_child(widget, index + offset)
        self.message_box.show_all()

        def preserve_view() -> bool:
            if (
                generation != self._room_load_generation
                or not self.active_room
                or int(self.active_room["id"]) != room_id
            ):
                return False
            new_upper = adjustment.get_upper()
            if scroll_serial == self._manual_scroll_serial:
                adjustment.set_value(
                    old_value + max(0.0, new_upper - old_upper)
                )
            self._loading_older_messages = False
            return False

        GLib.idle_add(preserve_view)
        return False

    def save_active_view_state(self) -> None:
        if not self.active_room or not hasattr(self, "message_scroll"):
            return
        adjustment = self.message_scroll.get_vadjustment()
        self.store.update_room(
            int(self.active_room["id"]),
            scroll_position=float(adjustment.get_value()),
            scroll_follow=1 if self.is_near_bottom(72.0) else 0,
        )

    def restore_scroll_position(
        self,
        value: float,
        generation: Optional[int] = None,
        scroll_serial: Optional[int] = None,
    ) -> None:
        def restore() -> bool:
            if generation is not None and generation != self._room_load_generation:
                return False
            if (
                scroll_serial is not None
                and scroll_serial != self._manual_scroll_serial
            ):
                return False
            adjustment = self.message_scroll.get_vadjustment()
            maximum = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
            adjustment.set_value(min(maximum, max(0.0, value)))
            return False

        GLib.idle_add(restore)

    def choose_image(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Choose an image",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Choose",
            Gtk.ResponseType.OK,
        )
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            image_filter.add_mime_type(mime)
        dialog.add_filter(image_filter)
        dialog.set_select_multiple(True)
        response = dialog.run()
        filenames = list(dialog.get_filenames() or [])
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            for filename in filenames:
                try:
                    imported = self.store.import_image(filename)
                    self.set_attachment(imported, Path(filename).name)
                except (GLib.Error, OSError, ValueError) as exc:
                    self.show_error("Could not add image", str(exc))

    def on_image_drop(
        self,
        _widget: Gtk.Widget,
        context: Gdk.DragContext,
        _x: int,
        _y: int,
        selection: Gtk.SelectionData,
        _info: int,
        event_time: int,
    ) -> None:
        uris = selection.get_uris() or []
        if not uris:
            Gtk.drag_finish(context, False, False, event_time)
            return
        success = False
        for uri in uris:
            try:
                filename, _hostname = GLib.filename_from_uri(uri)
                imported = self.store.import_image(filename)
                self.set_attachment(imported, Path(filename).name)
                success = True
            except (GLib.Error, OSError, TypeError, ValueError) as exc:
                self.show_error("Could not add image", str(exc))
        Gtk.drag_finish(context, success, False, event_time)

    def set_attachment(self, image_path: str, label: str, persist: bool = True) -> None:
        if len(self._pending_paths()) >= 12:
            self.store.remove_image_if_unused(image_path)
            raise ValueError("A message can contain up to 12 images.")
        try:
            inspect_attachment_image(image_path)
        except (AttachmentImageError, GLib.Error, OSError):
            self.store.remove_image_if_unused(image_path)
            raise
        if not hasattr(self, "pending_images"):
            self.pending_images = []
        if image_path not in self._pending_paths():
            self.pending_images.append({"path": image_path, "label": label})
        self._sync_pending_image_aliases()
        self._render_attachment_strip()
        if persist:
            self.save_active_draft()
        self.update_composer_state()

    def _pending_paths(self) -> List[str]:
        images = getattr(self, "pending_images", [])
        paths = [str(item.get("path")) for item in images if item.get("path")]
        legacy = getattr(self, "pending_image", None)
        if legacy and str(legacy) not in paths:
            paths.insert(0, str(legacy))
        return paths

    def _pending_labels(self) -> List[str]:
        images = getattr(self, "pending_images", [])
        labels = [str(item.get("label") or Path(str(item["path"])).name) for item in images]
        if not labels and getattr(self, "pending_image", None):
            labels = [str(getattr(self, "pending_image_label", None) or Path(self.pending_image).name)]
        return labels

    def _usable_pending_paths(self) -> List[str]:
        valid_paths: List[str] = []
        unavailable_paths: List[str] = []
        for path in self._pending_paths():
            try:
                inspect_attachment_image(path)
            except (AttachmentImageError, GLib.Error, OSError):
                unavailable_paths.append(path)
            else:
                valid_paths.append(path)
        if not unavailable_paths:
            return valid_paths
        if not self.pending_images and self.pending_image:
            self.pending_images = [
                {
                    "path": self.pending_image,
                    "label": self.pending_image_label
                    or Path(self.pending_image).name,
                }
            ]
        self.pending_images = [
            item
            for item in self.pending_images
            if str(item.get("path")) not in unavailable_paths
        ]
        self._sync_pending_image_aliases()
        self._render_attachment_strip()
        self.save_active_draft()
        for path in unavailable_paths:
            self.store.remove_image_if_unused(path)
        count = len(unavailable_paths)
        self.show_error(
            "Image removed",
            f"{count} unavailable image{' was' if count == 1 else 's were'} removed before sending.",
        )
        self.update_composer_state()
        return valid_paths

    def _sync_pending_image_aliases(self) -> None:
        images = getattr(self, "pending_images", [])
        self.pending_image = str(images[0]["path"]) if images else None
        self.pending_image_label = str(images[0]["label"]) if images else None

    def _render_attachment_strip(self) -> None:
        if not hasattr(self, "attachment_flow"):
            return
        render_generation = getattr(self, "_attachment_render_generation", 0) + 1
        self._attachment_render_generation = render_generation
        for child in self.attachment_flow.get_children():
            self.attachment_flow.remove(child)
        count = len(self.pending_images)
        self.attachment_name.set_text(
            f"{count} image{'s' if count != 1 else ''} attached · click a thumbnail to preview"
        )
        for item in self.pending_images:
            path = str(item["path"])
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.set_direction(Gtk.TextDirection.LTR)
            chip.get_style_context().add_class("attachment-chip")
            label = str(item.get("label") or Path(path).name)
            thumbnail = Gtk.Button()
            thumbnail.set_relief(Gtk.ReliefStyle.NONE)
            thumbnail.set_size_request(136, 92)
            thumbnail.set_tooltip_text(f"Loading preview for {label}")
            thumbnail.get_style_context().add_class("attachment-thumbnail")
            thumbnail.set_sensitive(False)
            placeholder = Gtk.Spinner()
            placeholder.start()
            thumbnail.add(placeholder)
            chip.pack_start(thumbnail, False, False, 0)
            self._load_attachment_thumbnail_async(
                path, label, thumbnail, render_generation
            )
            name = Gtk.Label(label=label)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_max_width_chars(16)
            name.get_style_context().add_class("attachment-chip-name")
            chip.pack_start(name, False, False, 0)
            remove = Gtk.Button.new_with_label("×")
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.get_style_context().add_class("attachment-chip-remove")
            remove.connect(
                "clicked", lambda _button, target=path: self.clear_attachment(image_path=target)
            )
            chip.pack_end(remove, False, False, 0)
            self.attachment_flow.add(chip)
        if count:
            self.attachment_bar.set_no_show_all(False)
            self.attachment_bar.show_all()
        else:
            self.attachment_bar.hide()
            self.attachment_bar.set_no_show_all(True)

    def _load_attachment_thumbnail_async(
        self,
        image_path: str,
        label: str,
        thumbnail: Gtk.Button,
        render_generation: int,
    ) -> None:
        def is_current() -> bool:
            return bool(
                not getattr(self, "_closing", False)
                and render_generation
                == getattr(self, "_attachment_render_generation", 0)
                and image_path in self._pending_paths()
                and thumbnail.get_parent() is not None
            )

        def set_image(_info: Any, pixbuf: GdkPixbuf.Pixbuf) -> None:
            if not is_current():
                return
            child = thumbnail.get_child()
            if child:
                thumbnail.remove(child)
            image = Gtk.Image.new_from_pixbuf(pixbuf)
            image.get_style_context().add_class("attachment-thumbnail-image")
            thumbnail.add(image)
            thumbnail.set_sensitive(True)
            thumbnail.set_tooltip_text(f"Preview {label}")
            thumbnail.connect(
                "clicked",
                lambda _button, target=image_path, name=label: self.show_attachment_preview(
                    target, name
                ),
            )
            thumbnail.show_all()

        def set_error(message: str) -> None:
            if not is_current():
                return
            child = thumbnail.get_child()
            if child:
                thumbnail.remove(child)
            missing = Gtk.Image.new_from_icon_name(
                "image-missing", Gtk.IconSize.DIALOG
            )
            missing.get_style_context().add_class("attachment-thumbnail-missing")
            thumbnail.add(missing)
            thumbnail.set_tooltip_text(f"Could not preview {label}: {message}")
            thumbnail.show_all()

        load_attachment_image_async(
            image_path, 136, 92, set_image, set_error
        )

    def show_attachment_preview(
        self, image_path: str, label: str
    ) -> Optional[Gtk.Dialog]:
        try:
            return show_attachment_preview(self, image_path, label)
        except (AttachmentImageError, GLib.Error, OSError) as exc:
            self.show_error("Could not preview image", str(exc))
            return None

    def clear_attachment(
        self, persist: bool = True, image_path: Optional[str] = None
    ) -> None:
        previous_images = self._pending_paths()
        if image_path is None:
            self.pending_images = []
        else:
            self.pending_images = [
                item for item in getattr(self, "pending_images", [])
                if str(item.get("path")) != image_path
            ]
        self._sync_pending_image_aliases()
        self._render_attachment_strip()
        if persist:
            self.save_active_draft()
            for previous_image in previous_images:
                if previous_image not in self._pending_paths():
                    self.store.remove_image_if_unused(previous_image)
        self.update_composer_state()

    def _set_composer_rtl(self, rtl: bool) -> None:
        if getattr(self, "_composer_active_line_rtl", None) != rtl:
            self.input_view.set_direction(
                Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR
            )
            self.input_view.set_justification(Gtk.Justification.LEFT)
            self._composer_active_line_rtl = rtl
        self.input_placeholder.set_halign(
            Gtk.Align.END if rtl else Gtk.Align.START
        )
        self.input_placeholder.set_direction(Gtk.TextDirection.LTR)

    def _is_generated_composer_marker(self, iterator: Gtk.TextIter) -> bool:
        marker_tag = getattr(self, "_composer_direction_marker_tag", None)
        return bool(
            marker_tag is not None
            and iterator.get_char() == COMPOSER_RTL_MARK
            and iterator.has_tag(marker_tag)
        )

    def _composer_line_bounds(
        self, text_buffer: Gtk.TextBuffer, line: int
    ) -> tuple[Gtk.TextIter, Gtk.TextIter]:
        line = max(0, min(line, text_buffer.get_line_count() - 1))
        start = text_buffer.get_iter_at_line(line)
        end = start.copy()
        end.forward_to_line_end()
        return start, end

    def _composer_line_is_rtl(
        self, text_buffer: Gtk.TextBuffer, iterator: Gtk.TextIter
    ) -> bool:
        start, _end = self._composer_line_bounds(text_buffer, iterator.get_line())
        return self._is_generated_composer_marker(start)

    def _composer_line_has_user_content(
        self, text_buffer: Gtk.TextBuffer, iterator: Gtk.TextIter
    ) -> bool:
        start, end = self._composer_line_bounds(text_buffer, iterator.get_line())
        while start.compare(end) < 0:
            if not self._is_generated_composer_marker(start):
                return True
            start.forward_char()
        return False

    def _composer_range_counts(
        self,
        text_buffer: Gtk.TextBuffer,
        start: Gtk.TextIter,
        end: Gtk.TextIter,
    ) -> tuple[int, int, int, bool, bool]:
        user_count = 0
        non_whitespace = 0
        generated_marker_count = 0
        has_newline = False
        has_rtl = False
        iterator = start.copy()
        while iterator.compare(end) < 0:
            character = iterator.get_char()
            if self._is_generated_composer_marker(iterator):
                generated_marker_count += 1
            else:
                user_count += 1
                if not character.isspace():
                    non_whitespace += 1
                if character == "\n":
                    has_newline = True
                if unicodedata.bidirectional(character) in {"R", "AL"}:
                    has_rtl = True
            iterator.forward_char()
        return (
            user_count,
            non_whitespace,
            generated_marker_count,
            has_newline,
            has_rtl,
        )

    def _on_composer_insert_text(
        self,
        text_buffer: Gtk.TextBuffer,
        iterator: Gtk.TextIter,
        text: str,
        _length: int,
    ) -> None:
        if getattr(self, "_syncing_composer_direction", False):
            return
        start_mark = text_buffer.create_mark(None, iterator, True)
        end_mark = text_buffer.create_mark(None, iterator, False)
        line_was_empty = not self._composer_line_has_user_content(
            text_buffer, iterator
        )
        self._composer_user_char_count = (
            getattr(self, "_composer_user_char_count", 0) + len(text)
        )
        self._composer_non_whitespace_count = (
            getattr(self, "_composer_non_whitespace_count", 0)
            + sum(not character.isspace() for character in text)
        )
        self._composer_direction_jobs.append(
            {
                "start": start_mark,
                "end": end_mark,
                "next_line": None,
                "mode": "insert",
                "inserted_text": text,
                "line_was_empty": line_was_empty,
                "force_scan": "\n" in text,
            }
        )

    def _on_composer_delete_range(
        self,
        text_buffer: Gtk.TextBuffer,
        start: Gtk.TextIter,
        end: Gtk.TextIter,
    ) -> None:
        if getattr(self, "_syncing_composer_direction", False):
            return
        user_count, non_whitespace, marker_count, has_newline, has_rtl = (
            self._composer_range_counts(text_buffer, start, end)
        )
        start_mark = text_buffer.create_mark(None, start, True)
        end_mark = text_buffer.create_mark(None, start, False)
        self._composer_user_char_count = max(
            0, getattr(self, "_composer_user_char_count", 0) - user_count
        )
        self._composer_non_whitespace_count = max(
            0,
            getattr(self, "_composer_non_whitespace_count", 0) - non_whitespace,
        )
        self._composer_generated_marker_count = max(
            0,
            getattr(self, "_composer_generated_marker_count", 0) - marker_count,
        )
        self._composer_direction_jobs.append(
            {
                "start": start_mark,
                "end": end_mark,
                "next_line": None,
                "mode": "delete",
                "inserted_text": "",
                "line_was_empty": False,
                # Removing text without strong RTL characters does not change
                # a nonempty line's direction. Avoid rescanning a large draft
                # for routine Backspace; inspect the line only when a direction
                # marker, newline, or strong RTL character was removed.
                "force_scan": bool(marker_count) or has_newline or has_rtl,
            }
        )

    def _composer_line_scan(
        self, text_buffer: Gtk.TextBuffer, line: int
    ) -> tuple[bool, List[Gtk.TextIter]]:
        start, end = self._composer_line_bounds(text_buffer, line)
        markers: List[Gtk.TextIter] = []
        has_text = False
        has_rtl = False
        has_non_whitespace = False
        iterator = start.copy()
        while iterator.compare(end) < 0:
            character = iterator.get_char()
            if self._is_generated_composer_marker(iterator):
                markers.append(iterator.copy())
            else:
                has_text = True
                has_rtl = has_rtl or unicodedata.bidirectional(character) in {"R", "AL"}
                has_non_whitespace = has_non_whitespace or not character.isspace()
            iterator.forward_char()
        return (not has_text or not has_non_whitespace or has_rtl), markers

    def _delete_composer_markers(
        self, text_buffer: Gtk.TextBuffer, markers: List[Gtk.TextIter]
    ) -> None:
        for iterator in reversed(markers):
            end = iterator.copy()
            end.forward_char()
            text_buffer.delete(iterator, end)
            self._composer_generated_marker_count = max(
                0, getattr(self, "_composer_generated_marker_count", 0) - 1
            )

    def _set_composer_line_tag(
        self, text_buffer: Gtk.TextBuffer, line: int, rtl: bool
    ) -> None:
        start, end = self._composer_line_bounds(text_buffer, line)
        if start.compare(end) >= 0:
            return
        first_end = start.copy()
        first_end.forward_char()
        text_buffer.remove_tag(
            self._composer_ltr_tag if rtl else self._composer_rtl_tag,
            start,
            first_end,
        )
        text_buffer.apply_tag(
            self._composer_rtl_tag if rtl else self._composer_ltr_tag,
            start,
            first_end,
        )

    def _normalise_composer_line(
        self, line: int, job: Dict[str, Any]
    ) -> bool:
        text_buffer = self.input_view.get_buffer()
        start, end = self._composer_line_bounds(text_buffer, line)
        line_length = end.get_offset() - start.get_offset()
        force_scan = bool(job.get("force_scan"))
        mode = str(job.get("mode") or "insert")
        if force_scan or line_length <= 512:
            rtl, markers = self._composer_line_scan(text_buffer, line)
        else:
            markers = [start.copy()] if self._is_generated_composer_marker(start) else []
            if mode == "insert":
                if job.get("line_was_empty"):
                    rtl = composer_paragraph_is_rtl(str(job.get("inserted_text") or ""))
                elif markers:
                    rtl = True
                else:
                    rtl = any(
                        unicodedata.bidirectional(character) in {"R", "AL"}
                        for character in str(job.get("inserted_text") or "")
                    )
            else:
                rtl = bool(markers) or not self._composer_line_has_user_content(
                    text_buffer, start
                )
        leading_marker = bool(
            markers
            and markers[0].get_offset() == start.get_offset()
        )
        internal_markers = markers[1:] if leading_marker else markers
        if internal_markers:
            self._delete_composer_markers(text_buffer, internal_markers)
            start, end = self._composer_line_bounds(text_buffer, line)
            leading_marker = self._is_generated_composer_marker(start)
        empty_buffer = (
            text_buffer.get_line_count() == 1
            and line == 0
            and start.compare(end) >= 0
        )
        if rtl and not leading_marker and not empty_buffer:
            text_buffer.insert_with_tags(
                start,
                COMPOSER_RTL_MARK,
                self._composer_direction_marker_tag,
                self._composer_rtl_tag,
            )
            self._composer_generated_marker_count = (
                getattr(self, "_composer_generated_marker_count", 0) + 1
            )
        elif not rtl and leading_marker:
            self._delete_composer_markers(text_buffer, [start])
        self._set_composer_line_tag(text_buffer, line, rtl)
        return rtl

    def _discard_composer_direction_job(
        self, text_buffer: Gtk.TextBuffer, job: Dict[str, Any]
    ) -> None:
        for name in ("start", "end"):
            mark = job.get(name)
            if mark is not None:
                text_buffer.delete_mark(mark)

    def _run_composer_direction_jobs(self, limit: int) -> bool:
        text_buffer = self.input_view.get_buffer()
        processed = 0
        while self._composer_direction_jobs and processed < limit:
            job = self._composer_direction_jobs[0]
            if job.get("next_line") is None:
                start = text_buffer.get_iter_at_mark(job["start"])
                end = text_buffer.get_iter_at_mark(job["end"])
                job["next_line"] = start.get_line()
                job["last_line"] = max(start.get_line(), end.get_line())
            line = int(job["next_line"])
            if line > int(job["last_line"]):
                self._discard_composer_direction_job(text_buffer, job)
                self._composer_direction_jobs.pop(0)
                continue
            rtl = self._normalise_composer_line(line, job)
            insert = text_buffer.get_iter_at_mark(text_buffer.get_insert())
            if insert.get_line() == line:
                self._set_composer_rtl(rtl)
            job["next_line"] = line + 1
            processed += 1
        return bool(self._composer_direction_jobs)

    def _drain_composer_direction_jobs(self) -> bool:
        self._composer_direction_idle_source_id = None
        if self._closing:
            return False
        self._syncing_composer_direction = True
        try:
            remaining = self._run_composer_direction_jobs(
                COMPOSER_DIRECTION_IDLE_BATCH_SIZE
            )
        finally:
            self._syncing_composer_direction = False
        if remaining:
            self._composer_direction_idle_source_id = GLib.idle_add(
                self._drain_composer_direction_jobs
            )
        return False

    def _schedule_composer_direction_jobs(self) -> None:
        if (
            self._composer_direction_jobs
            and self._composer_direction_idle_source_id is None
        ):
            self._composer_direction_idle_source_id = GLib.idle_add(
                self._drain_composer_direction_jobs
            )

    def _clear_composer_direction_jobs(self) -> None:
        if self._composer_direction_idle_source_id is not None:
            GLib.source_remove(self._composer_direction_idle_source_id)
            self._composer_direction_idle_source_id = None
        text_buffer = self.input_view.get_buffer()
        for job in self._composer_direction_jobs:
            self._discard_composer_direction_job(text_buffer, job)
        self._composer_direction_jobs.clear()

    def _sync_composer_direction_markers(self, rtl: bool) -> None:
        text_buffer = self.input_view.get_buffer()
        job = {
            "mode": "sync",
            "inserted_text": "",
            "line_was_empty": False,
            "force_scan": True,
        }
        for line in range(text_buffer.get_line_count()):
            self._normalise_composer_line(line, job)

    def _composer_user_offset(self, iterator: Gtk.TextIter) -> int:
        raw_offset = iterator.get_offset()
        if not getattr(self, "_composer_generated_marker_count", 0):
            return raw_offset
        text_buffer = self.input_view.get_buffer()
        cursor = text_buffer.get_start_iter()
        generated_before = 0
        while cursor.compare(iterator) < 0:
            if self._is_generated_composer_marker(cursor):
                generated_before += 1
            cursor.forward_char()
        return max(0, raw_offset - generated_before)

    def _composer_snapshot(self) -> tuple[str, int, int]:
        text_buffer = self.input_view.get_buffer()
        insert = text_buffer.get_iter_at_mark(text_buffer.get_insert())
        bound = text_buffer.get_iter_at_mark(text_buffer.get_selection_bound())
        return (
            self._input_text(),
            self._composer_user_offset(insert),
            self._composer_user_offset(bound),
        )

    def _composer_iter_at_user_offset(self, offset: int) -> Gtk.TextIter:
        text_buffer = self.input_view.get_buffer()
        iterator = text_buffer.get_start_iter()
        end = text_buffer.get_end_iter()
        user_offset = 0
        target = max(0, offset)
        while iterator.compare(end) < 0:
            if self._is_generated_composer_marker(iterator):
                iterator.forward_char()
                continue
            if user_offset >= target:
                break
            user_offset += 1
            iterator.forward_char()
        return iterator

    def _apply_composer_snapshot(self, snapshot: tuple[str, int, int]) -> None:
        text, insert_offset, bound_offset = snapshot
        self._restoring_composer_history = True
        try:
            text_buffer = self.input_view.get_buffer()
            self._clear_composer_direction_jobs()
            text_buffer.set_text(text)
            self._syncing_composer_direction = True
            try:
                while self._composer_direction_jobs:
                    self._run_composer_direction_jobs(256)
            finally:
                self._syncing_composer_direction = False
            insert = self._composer_iter_at_user_offset(insert_offset)
            bound = self._composer_iter_at_user_offset(bound_offset)
            text_buffer.select_range(insert, bound)
            self.input_view.scroll_mark_onscreen(text_buffer.get_insert())
        finally:
            self._restoring_composer_history = False

    def _reset_composer_history(self) -> None:
        self._composer_undo_stack.clear()
        self._composer_redo_stack.clear()
        self._composer_history_before = None
        self._composer_history_depth = 0

    def _on_composer_user_action_begin(self, _buffer: Gtk.TextBuffer) -> None:
        if self._restoring_composer_history:
            return
        if self._composer_history_depth == 0:
            self._composer_history_before = self._composer_snapshot()
        self._composer_history_depth += 1

    def _on_composer_user_action_end(self, _buffer: Gtk.TextBuffer) -> None:
        if self._restoring_composer_history or self._composer_history_depth <= 0:
            return
        self._composer_history_depth -= 1
        if self._composer_history_depth:
            return
        before = self._composer_history_before
        self._composer_history_before = None
        after = self._composer_snapshot()
        if before is None or before == after:
            return
        self._composer_undo_stack.append(before)
        if len(self._composer_undo_stack) > self._composer_history_limit:
            del self._composer_undo_stack[0]
        self._composer_redo_stack.clear()

    def _undo_composer_edit(self) -> bool:
        if not self._composer_undo_stack:
            return False
        current = self._composer_snapshot()
        target = self._composer_undo_stack.pop()
        self._composer_redo_stack.append(current)
        self._apply_composer_snapshot(target)
        return True

    def _redo_composer_edit(self) -> bool:
        if not self._composer_redo_stack:
            return False
        current = self._composer_snapshot()
        target = self._composer_redo_stack.pop()
        self._composer_undo_stack.append(current)
        self._apply_composer_snapshot(target)
        return True

    def on_draft_changed(self, _buffer: Gtk.TextBuffer) -> None:
        if getattr(self, "_syncing_composer_direction", False):
            return
        self._syncing_composer_direction = True
        try:
            self._run_composer_direction_jobs(COMPOSER_DIRECTION_IDLE_BATCH_SIZE)
        finally:
            self._syncing_composer_direction = False
        self._schedule_composer_direction_jobs()
        self.update_composer_state()
        if self._loading_room:
            return
        if self._draft_save_source_id is not None:
            GLib.source_remove(self._draft_save_source_id)
        self._draft_save_source_id = GLib.timeout_add(350, self._save_draft_timeout)

    def _save_draft_timeout(self) -> bool:
        self._draft_save_source_id = None
        self.save_active_draft()
        return False

    def save_active_draft(self) -> None:
        if self._loading_room or not self.active_room:
            return
        self.store.update_room(
            int(self.active_room["id"]),
            draft_text=self._input_text(),
            draft_image_paths=self._pending_paths(),
            draft_image_labels=self._pending_labels(),
            draft_reply_to_id=(
                int(self.pending_reply["id"]) if self.pending_reply else None
            ),
            draft_reply_quote=(
                str(self.pending_reply["content"])
                if self.pending_reply
                else None
            ),
        )

    def _reset_enter_send_confirmation(self) -> bool:
        """Clear the short-lived second-Enter confirmation state."""
        self._enter_send_armed_until = 0.0
        if self._enter_send_hint_source_id is not None:
            GLib.source_remove(self._enter_send_hint_source_id)
            self._enter_send_hint_source_id = None
        composer_notice_source_id = getattr(
            self, "_composer_notice_source_id", None
        )
        if composer_notice_source_id is not None:
            GLib.source_remove(composer_notice_source_id)
            self._composer_notice_source_id = None
        if hasattr(self, "composer_hint"):
            self.composer_hint.set_text(COMPOSER_HINT)
        return False

    def _expire_enter_send_confirmation(self) -> bool:
        self._enter_send_hint_source_id = None
        return self._reset_enter_send_confirmation()

    def _confirm_send_with_enter(self) -> bool:
        """Send only when Enter is pressed twice in a short confirmation window."""
        now = time.monotonic()
        if now <= self._enter_send_armed_until:
            self._reset_enter_send_confirmation()
            self.send_message()
            return True
        self._reset_enter_send_confirmation()
        self._enter_send_armed_until = now + 1.25
        if hasattr(self, "composer_hint"):
            self.composer_hint.set_text("Press Enter again to send")
        self._enter_send_hint_source_id = GLib.timeout_add(
            1250, self._expire_enter_send_confirmation
        )
        return True

    def on_input_key(self, _view: Gtk.TextView, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_BackSpace and not event.state & (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        ):
            text_buffer = self.input_view.get_buffer()
            cursor = text_buffer.get_iter_at_mark(text_buffer.get_insert())
            previous = cursor.copy()
            if previous.backward_char() and self._is_generated_composer_marker(
                previous
            ):
                newline = previous.copy()
                if newline.backward_char() and newline.get_char() == "\n":
                    text_buffer.begin_user_action()
                    try:
                        text_buffer.delete(newline, previous)
                    finally:
                        text_buffer.end_user_action()
                    return True
        # Choose alignment before GTK paints the first character.  This avoids
        # the visible left-to-right flash when an RTL draft starts while an
        # LTR keyboard layout had been active moments earlier.
        composer_char_count = getattr(self, "_composer_user_char_count", None)
        composer_is_empty = (
            not composer_char_count
            if composer_char_count is not None
            else not self._input_text()
        )
        if composer_is_empty and not event.state & (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        ):
            codepoint = Gdk.keyval_to_unicode(event.keyval)
            if codepoint:
                first_character = chr(codepoint)
                if first_character.strip():
                    self._set_composer_rtl(composer_is_rtl(first_character))
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            keyval = event.keyval
            keymap = Gdk.Keymap.get_for_display(self.input_view.get_display())
            translated = keymap.translate_keyboard_state(
                event.hardware_keycode, Gdk.ModifierType(0), 0
            )
            if translated[0]:
                keyval = translated[1]
            blocked_modifiers = (
                Gdk.ModifierType.MOD1_MASK
                | Gdk.ModifierType.SUPER_MASK
                | Gdk.ModifierType.META_MASK
                | Gdk.ModifierType.HYPER_MASK
            )
            if not event.state & blocked_modifiers and keyval in (
                Gdk.KEY_z,
                Gdk.KEY_Z,
            ):
                self._reset_enter_send_confirmation()
                if event.state & Gdk.ModifierType.SHIFT_MASK:
                    self._redo_composer_edit()
                else:
                    self._undo_composer_edit()
                return True
            if (
                not event.state & blocked_modifiers
                and keyval in (Gdk.KEY_y, Gdk.KEY_Y)
            ):
                self._reset_enter_send_confirmation()
                self._redo_composer_edit()
                return True
            if keyval in (Gdk.KEY_v, Gdk.KEY_V):
                if self.paste_clipboard_image():
                    return True
        if event.keyval in SEND_ENTER_KEYS:
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                self._reset_enter_send_confirmation()
                return False
            non_whitespace = getattr(self, "_composer_non_whitespace_count", None)
            has_text = (
                bool(non_whitespace)
                if non_whitespace is not None
                else bool(self._input_text().strip())
            )
            if not has_text and not self.pending_image:
                self._reset_enter_send_confirmation()
                return False
            return self._confirm_send_with_enter()
        if self._enter_send_armed_until:
            self._reset_enter_send_confirmation()
        return False

    def on_paste_clipboard(self, _view: Gtk.TextView) -> None:
        if self.paste_clipboard_image():
            self.input_view.stop_emission_by_name("paste-clipboard")

    def paste_clipboard_image(self) -> bool:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if clipboard.wait_is_image_available():
            pixbuf = clipboard.wait_for_image()
            if pixbuf is not None:
                self.attach_clipboard_image(pixbuf)
                return True

        for uri in clipboard.wait_for_uris() or []:
            try:
                filename, _hostname = GLib.filename_from_uri(uri)
            except GLib.Error:
                continue
            if filename and Path(filename).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif"
            }:
                try:
                    imported = self.store.import_image(filename)
                    self.set_attachment(imported, Path(filename).name)
                    return True
                except (GLib.Error, OSError, ValueError) as exc:
                    self.show_error("Could not add image", str(exc))
                    return True
        return False

    def attach_clipboard_image(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_path = temp_file.name
            pixbuf.savev(temp_path, "png", [], [])
            imported = self.store.import_image(temp_path)
            self.set_attachment(imported, "Clipboard image")
        except (GLib.Error, OSError, ValueError) as exc:
            self.show_error("Could not add image", str(exc))
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink()
                except FileNotFoundError:
                    pass

    def _input_text(self) -> str:
        buffer = self.input_view.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        if not getattr(self, "_composer_generated_marker_count", 0):
            return buffer.get_text(start, end, True)
        pieces: List[str] = []
        chunk_start = start.copy()
        iterator = start.copy()
        while iterator.compare(end) < 0:
            if self._is_generated_composer_marker(iterator):
                pieces.append(buffer.get_text(chunk_start, iterator, True))
                iterator.forward_char()
                chunk_start = iterator.copy()
            else:
                iterator.forward_char()
        pieces.append(buffer.get_text(chunk_start, end, True))
        return "".join(pieces)

    def send_message(self) -> None:
        if not self.active_room:
            return
        room_id = int(self.active_room["id"])
        if room_id in self.room_turns:
            self.steer_active_turn(room_id)
            return
        text = self._input_text().strip()
        if not text and not self.pending_image:
            return
        if not self.connected:
            self.show_error(
                "Connection unavailable",
                "Send the message after Codex reconnects.",
            )
            return
        if not self.models or not self.model_combo.get_active_id():
            self.show_error("Model unavailable", "Wait for the model list to load.")
            return

        image_paths = self._usable_pending_paths()
        if not text and not image_paths:
            return
        image_path = image_paths[0] if image_paths else None
        reply = self.pending_reply
        reply_to_id = int(reply["id"]) if reply else None
        user_message_id = self.store.add_message(
            room_id,
            "user",
            text,
            image_path,
            reply_to_id=reply_to_id,
            reply_quote=str(reply["content"]) if reply else None,
            image_paths=image_paths,
        )
        bubble = MessageBubble(
            "user",
            text or "Please review this image.",
            image_path,
            message_id=user_message_id,
            reply_preview=reply.get("preview") if reply else None,
            on_reply=self.start_reply,
            on_branch=self.open_side_thread,
            image_paths=image_paths,
        )
        self.message_bubbles[user_message_id] = bubble
        self.message_box.pack_start(bubble, False, False, 0)
        bubble.show_all()

        if str(self.active_room.get("title") or "") == "New chat" and text:
            title = " ".join(text.split())[:48]
            renamed = self.store.rename_room(room_id, title)
            if renamed:
                self.active_room = renamed
                self.header.set_title(str(renamed["title"]))

        buffer = self.input_view.get_buffer()
        self._clear_composer_direction_jobs()
        buffer.set_text("")
        self._reset_composer_history()
        self.clear_attachment()
        self.clear_reply()
        self.store.update_room(
            room_id, draft_text="", draft_image_paths=[], draft_image_labels=[],
            draft_reply_to_id=None,
            draft_reply_quote=None,
        )

        model = str(self.model_combo.get_active_id())
        effort = self.effort_combo.get_active_id()
        self.store.update_room(room_id, model=model, effort=effort)
        self.active_room["model"] = model
        self.active_room["effort"] = effort

        thread_id = self.active_room.get("thread_id")
        prompt_text = text
        if reply:
            quoted = str(reply.get("content") or reply.get("preview") or "")[:1600]
            prompt_text = f"In reply to this message:\n> {quoted}\n\n{text}".strip()
        state: Dict[str, Any] = {
            "room_id": room_id,
            "thread_id": str(thread_id) if thread_id else None,
            "turn_id": None,
            "model": model,
            "effort": effort,
            "text": text,
            "prompt_text": prompt_text,
            "image_path": image_path,
            "image_paths": image_paths,
            "streaming_text": "",
            "streaming_item_id": "",
            "streaming_items": [],
            "streaming_anonymous_item_counter": 0,
            "final_text": "",
            "completed_agent_text": "",
            "started_at": time.monotonic(),
            "stop_requested": False,
            "activity_text": "",
            "reasoning_summary": "",
            "activity_feed": ActivityFeed(),
            "activity_snapshot": None,
            "activity_presentation": "working",
            "render_source_id": None,
            "final_answer_source_id": None,
            "final_answer_generation": 0,
            "connection_lost_at": None,
            "bubble": MessageBubble(
                "assistant",
                "",
                streaming=True,
                on_reply=self.start_reply,
                on_branch=self.open_side_thread,
            ),
        }
        self.room_turns[room_id] = state
        self.message_box.pack_start(state["bubble"], False, False, 0)
        state["bubble"].show_all()
        self.sync_active_room_controls()
        self.refresh_rooms()
        self.scroll_to_bottom()

        if not thread_id:
            self.bridge.start_thread(
                model,
                DEFAULT_CWD,
                lambda result, rid=room_id: self._thread_started(rid, result),
                lambda error, rid=room_id: self._room_turn_error(rid, error),
                room_id=room_id,
            )
        elif str(thread_id) not in self.loaded_threads:
            self.bridge.resume_thread(
                str(thread_id),
                model,
                DEFAULT_CWD,
                lambda result, rid=room_id: self._thread_resumed(rid, result),
                lambda error, rid=room_id: self._room_turn_error(rid, error),
                room_id=room_id,
            )
        else:
            self._start_turn(room_id, str(thread_id))

    def steer_active_turn(self, room_id: int) -> None:
        """Append a follow-up to the in-flight turn without corrupting its model state."""
        state = self.room_turns.get(room_id)
        if not state or not state.get("thread_id") or not state.get("turn_id"):
            self.show_error(
                "Not ready yet",
                "Wait a moment for the response turn to start.",
            )
            return
        text = self._input_text().strip()
        image_paths = self._usable_pending_paths()
        image_path = image_paths[0] if image_paths else None
        if not text and not image_paths:
            return
        reply = self.pending_reply
        prompt_text = text
        if reply:
            quoted = str(reply.get("content") or reply.get("preview") or "")[:1600]
            prompt_text = f"In reply to this message:\n> {quoted}\n\n{text}".strip()
        message_id = self.store.add_message(
            room_id,
            "user",
            text,
            image_path,
            reply_to_id=int(reply["id"]) if reply else None,
            reply_quote=str(reply["content"]) if reply else None,
            image_paths=image_paths,
        )
        bubble = MessageBubble(
            "user",
            text or "Please review this image too.",
            image_path,
            message_id=message_id,
            reply_preview=reply.get("preview") if reply else None,
            on_reply=self.start_reply,
            on_branch=self.open_side_thread,
            image_paths=image_paths,
        )
        self.message_bubbles[message_id] = bubble
        self.message_box.pack_start(bubble, False, False, 0)
        running_bubble = state.get("bubble")
        if running_bubble and running_bubble.get_parent() is self.message_box:
            self.message_box.reorder_child(running_bubble, -1)
        bubble.show_all()
        self._clear_composer_direction_jobs()
        self.input_view.get_buffer().set_text("")
        self._reset_composer_history()
        self.clear_attachment()
        self.clear_reply()
        self.store.update_room(
            room_id,
            draft_text="",
            draft_image_paths=[],
            draft_image_labels=[],
            draft_reply_to_id=None,
            draft_reply_quote=None,
        )
        record_state_activity(
            state,
            stage="Guidance",
            title="New guidance received",
            detail="Updating the response",
            source="turn/steer",
            key="steer",
        )
        self.bridge.steer_turn(
            str(state["thread_id"]),
            str(state["turn_id"]),
            prompt_text or "Please review this image too.",
            image_paths,
            lambda _result: None,
            lambda error: self.show_error(
                "Could not apply the new guidance", str(error.get("message") or error)
            ),
            room_id=room_id,
        )
        self.scroll_to_bottom()
        self.sync_active_room_controls()

    def _thread_started(self, room_id: int, result: Dict[str, Any]) -> bool:
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        state = self.room_turns.get(room_id)
        if not thread_id or not state:
            self._room_turn_error(room_id, {"message": "Codex did not return a thread ID."})
            return False
        self.store.update_room(room_id, thread_id=thread_id)
        if self.active_room and int(self.active_room["id"]) == room_id:
            self.active_room["thread_id"] = thread_id
        state["thread_id"] = str(thread_id)
        self.loaded_threads.add(str(thread_id))
        room = self.store.get_room(room_id)
        if room and room.get("title") and getattr(self, "connected", False):
            self.bridge.set_thread_name(
                str(thread_id), str(room["title"]), errback=lambda _error: None
            )
        self._start_turn(room_id, str(thread_id))
        return False

    def _thread_resumed(self, room_id: int, result: Dict[str, Any]) -> bool:
        state = self.room_turns.get(room_id)
        if not state:
            return False
        thread = result.get("thread") or {}
        thread_id = thread.get("id") or state.get("thread_id")
        if not thread_id:
            self._room_turn_error(room_id, {"message": "The previous thread could not be resumed."})
            return False
        state["thread_id"] = str(thread_id)
        self.loaded_threads.add(str(thread_id))
        self._start_turn(room_id, str(thread_id))
        return False

    def _start_turn(self, room_id: int, thread_id: str) -> None:
        state = self.room_turns.get(room_id)
        if not state:
            return
        self.bridge.start_turn(
            thread_id,
            state.get("prompt_text") or state["text"] or "Please review this image.",
            state["model"],
            state["effort"],
            DEFAULT_CWD,
            state.get("image_paths") or state.get("image_path"),
            lambda result, rid=room_id: self._turn_accepted(rid, result),
            lambda error, rid=room_id: self._room_turn_error(rid, error),
            room_id=room_id,
        )

    def _turn_accepted(self, room_id: int, result: Dict[str, Any]) -> bool:
        state = self.room_turns.get(room_id)
        if not state:
            return False
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            self._room_turn_error(
                room_id,
                {"message": "Codex did not return a turn ID."},
            )
            return False
        state["turn_id"] = str(turn_id)
        if state["stop_requested"] and state.get("thread_id") and state.get("turn_id"):
            self.bridge.interrupt(state["thread_id"], state["turn_id"])
        self.sync_active_room_controls()
        self.refresh_rooms()
        return False

    def _restore_orphaned_turn(self, room_id: int) -> Optional[Dict[str, Any]]:
        """Rebuild the minimum UI state after the GUI reconnects mid-request."""
        existing = self.room_turns.get(room_id)
        if existing:
            return existing
        room = self.store.get_room(room_id)
        if not room:
            return None
        messages = self.store.list_messages(room_id)
        # A pending request always leaves its user message last.  Refusing to
        # resurrect older user messages prevents a delayed/stale event from
        # creating another assistant bubble after a real terminal response.
        user_message = messages[-1] if messages else None
        if not user_message or user_message["role"] != "user":
            return None
        restored_text = str(user_message.get("content") or "")
        restored_prompt = restored_text
        if user_message.get("reply_to_id"):
            target = self.store.get_message(int(user_message["reply_to_id"]))
            if target:
                quoted = str(
                    user_message.get("reply_quote")
                    or target.get("content")
                    or ""
                )[:1600]
                restored_prompt = (
                    f"In reply to this message:\n> {quoted}\n\n{restored_text}"
                ).strip()
        state: Dict[str, Any] = {
            "room_id": room_id,
            "thread_id": room.get("thread_id"),
            "turn_id": None,
            "model": room.get("model"),
            "effort": room.get("effort"),
            "text": restored_text,
            "prompt_text": restored_prompt,
            "image_path": user_message.get("image_path"),
            "image_paths": list(user_message.get("image_paths") or []),
            "streaming_text": "",
            "streaming_item_id": "",
            "streaming_items": [],
            "streaming_anonymous_item_counter": 0,
            "final_text": "",
            "completed_agent_text": "",
            "activity_text": "Restoring activity…",
            "reasoning_summary": "",
            "activity_feed": ActivityFeed(),
            "activity_snapshot": None,
            "activity_presentation": "restoring",
            "render_source_id": None,
            "final_answer_source_id": None,
            "final_answer_generation": 0,
            "connection_lost_at": None,
            "started_at": time.monotonic(),
            "stop_requested": False,
            "bubble": None,
        }
        if self.active_room and int(self.active_room["id"]) == room_id:
            state["bubble"] = MessageBubble(
                "assistant",
                "",
                streaming=True,
                on_reply=self.start_reply,
                on_branch=self.open_side_thread,
            )
            self.message_box.pack_start(state["bubble"], False, False, 0)
            state["bubble"].show_all()
            set_state_activity_presentation(state, "restoring")
        self.room_turns[room_id] = state
        self.sync_active_room_controls()
        self.refresh_rooms()
        return state

    def _handle_orphan_response(self, params: Dict[str, Any]) -> None:
        context = params.get("context") or {}
        room_id = context.get("roomId")
        operation = params.get("requestMethod") or context.get("operation")
        if not room_id or operation not in {"thread/start", "thread/resume", "turn/start"}:
            return
        room_id = int(room_id)
        if not self._restore_orphaned_turn(room_id):
            return
        error = params.get("error")
        if error:
            self._room_turn_error(room_id, error)
            return
        result = params.get("result") or {}
        if operation == "thread/start":
            self._thread_started(room_id, result)
        elif operation == "thread/resume":
            self._thread_resumed(room_id, result)
        else:
            self._turn_accepted(room_id, result)

    def stop_generation(self, _button: Gtk.Button) -> None:
        if not self.active_room:
            return
        room_id = int(self.active_room["id"])
        state = self.room_turns.get(room_id)
        if not state:
            return
        state["stop_requested"] = True
        set_state_activity_presentation(state, "stopping")
        self.sync_active_room_controls()
        self.refresh_rooms()
        if state.get("thread_id") and state.get("turn_id"):
            self.bridge.interrupt(state["thread_id"], state["turn_id"])

    def _state_for_event(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        turn = params.get("turn") or {}
        item = params.get("item") or {}
        thread_id = params.get("threadId") or turn.get("threadId") or item.get("threadId")
        turn_id = params.get("turnId") or turn.get("id") or item.get("turnId")
        if turn_id:
            for state in self.room_turns.values():
                if state.get("turn_id") == turn_id:
                    return state
        if thread_id:
            for state in self.room_turns.values():
                if state.get("thread_id") != str(thread_id):
                    continue
                if state.get("turn_id") is None:
                    if turn_id:
                        state["turn_id"] = turn_id
                    return state
                if not turn_id:
                    return state
                # A different turn id on the same thread is a stale event.
                return None
        if thread_id:
            room = self.store.get_room_by_thread_id(str(thread_id))
            if room:
                room_id = int(room["id"])
                if room_id in self.room_turns:
                    return None
                restored = self._restore_orphaned_turn(room_id)
                if restored and turn_id:
                    restored["turn_id"] = str(turn_id)
                return restored
        return None

    def _state_for_usage_event(
        self, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Route metrics without binding a pending turn to restored old usage."""
        turn_id = params.get("turnId") or params.get("turn_id")
        if turn_id:
            exact = next(
                (
                    state
                    for state in self.room_turns.values()
                    if state.get("turn_id") == turn_id
                ),
                None,
            )
            if exact is not None:
                return exact
            thread_id = str(
                params.get("threadId") or params.get("thread_id") or ""
            )
            if any(
                state.get("thread_id") == thread_id
                for state in self.room_turns.values()
            ):
                return None
            # With no pending state for this thread, this can be the first
            # replayed event after a GUI reconnect. Let normal routing rebuild
            # the orphaned turn from the persisted thread mapping.
            return self._state_for_event(params)
        return self._state_for_event(params)

    def on_codex_event(self, method: str, params: Dict[str, Any]) -> bool:
        if getattr(self, "_closing", False):
            return False
        branch_controller = getattr(self, "branch_controller", None)
        if branch_controller and branch_controller.handle_event(method, params):
            return False
        if method == "backend/orphanResponse":
            self._handle_orphan_response(params)

        elif method == "item/agentMessage/delta":
            state = self._state_for_event(params)
            if state:
                append_agent_message_delta(state, params)
                self._queue_stream_render(int(state["room_id"]))

        elif method == "item/reasoning/summaryTextDelta":
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method == "turn/plan/updated":
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method == "turn/diff/updated":
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method in ("thread/tokenUsage/updated", "rawResponse/completed"):
            usage_router = getattr(self, "_state_for_usage_event", None)
            state = (
                usage_router(params)
                if usage_router is not None
                else self._state_for_event(params)
            )
            if not hasattr(self, "_thread_token_totals"):
                self._thread_token_totals = {}
            if state:
                total_tokens = update_turn_token_usage(
                    state,
                    params,
                    self._thread_token_totals,
                    raw_response=method == "rawResponse/completed",
                )
                if state.get("bubble") and total_tokens is not None:
                    state["bubble"].set_token_usage(total_tokens)
            elif method == "thread/tokenUsage/updated":
                # Resume/fork may replay a completed turn's lifetime snapshot
                # before the next turn starts. Preserve it only as the baseline
                # for future turn-local attribution.
                snapshot = token_usage_snapshot(params)
                cumulative = snapshot["cumulative"]
                thread_id = str(
                    params.get("threadId") or params.get("thread_id") or ""
                )
                previous = self._thread_token_totals.get(thread_id)
                if (
                    thread_id
                    and cumulative is not None
                    and (previous is None or cumulative >= previous)
                ):
                    self._thread_token_totals[thread_id] = cumulative

        elif method == "item/mcpToolCall/progress":
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method == "item/started":
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method in (
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        ):
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)

        elif method == "item/completed":
            item = params.get("item") or {}
            state = self._state_for_event(params)
            if state:
                ingest_state_activity(state, method, params)
            if state and item.get("type") == "agentMessage":
                text = str(item.get("text") or "")
                phase = str(item.get("phase") or "").strip().lower()
                if phase == "final_answer":
                    state["final_text"] = (
                        text
                        or str(state.get("streaming_text") or "")
                        or str(state.get("completed_agent_text") or "")
                    )
                    if state.get("bubble"):
                        follow = self.is_near_bottom()
                        source_id = state.get("render_source_id")
                        if source_id is not None:
                            GLib.source_remove(source_id)
                            state["render_source_id"] = None
                        state["bubble"].set_text(state["final_text"])
                        if follow:
                            self.scroll_to_bottom(force=False)
                    self._schedule_final_answer_fallback(int(state["room_id"]))
                else:
                    previous_text = str(state.get("streaming_text") or "")
                    streaming_text = reconcile_completed_agent_message(
                        state, params, item
                    )
                    if state.get("bubble") and streaming_text != previous_text:
                        follow = self.is_near_bottom()
                        state["bubble"].set_text(streaming_text)
                        if follow:
                            self.scroll_to_bottom(force=False)

        elif method == "turn/completed":
            turn = params.get("turn") or {}
            state = self._state_for_event(params)
            if state:
                error = turn.get("error") or {}
                self.finish_turn(int(state["room_id"]), error_text=error.get("message"))

        elif method == "error":
            error = params.get("error") or {}
            message = error.get("message") or "Unknown Codex error"
            state = self._state_for_event(params)
            if state:
                self.finish_turn(int(state["room_id"]), error_text=message)
            else:
                self.set_status("Error", "error")

        elif method in ("warning", "configWarning"):
            message = params.get("message") or params.get("summary")
            if message:
                self.set_status("Warning", "warning")

        elif method == "bridge/closed":
            self.connected = False
            for state in self.room_turns.values():
                self._mark_turn_reconnecting(
                    state,
                    "The response is waiting for Codex to reconnect.",
                    source=method,
                )
            self.set_status("Reconnecting…", "connecting")
            self.sync_active_room_controls()
            self.refresh_rooms()
            self.schedule_reconnect()
        return False

    def _queue_stream_render(self, room_id: int) -> None:
        state = self.room_turns.get(room_id)
        if not state or not state.get("bubble") or state.get("render_source_id") is not None:
            return

        def render() -> bool:
            current = self.room_turns.get(room_id)
            if current is not state:
                return False
            current["render_source_id"] = None
            bubble = current.get("bubble")
            if bubble:
                follow = self.is_near_bottom()
                bubble.set_text(current.get("streaming_text") or "")
                if follow:
                    self.scroll_to_bottom(force=False)
            return False

        state["render_source_id"] = GLib.timeout_add(100, render)

    def on_server_request(
        self, request_id: int, method: str, params: Dict[str, Any]
    ) -> bool:
        # The user explicitly requested full-access operation.  Command/file
        # prompts should therefore be rare, but accepting the session-scoped
        # request keeps the custom client from deadlocking on policy prompts.
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            self.bridge.respond(request_id, {"decision": "acceptForSession"})
        elif method == "item/permissions/requestApproval":
            requested = params.get("permissions") or params.get("requestedPermissions") or {}
            self.bridge.respond(
                request_id, {"permissions": requested, "scope": "session"}
            )
        elif method == "item/tool/requestUserInput":
            self.answer_user_input_request(request_id, params)
        elif method == "mcpServer/elicitation/request":
            self.answer_mcp_elicitation(request_id, params)
        else:
            self.bridge.respond_error(
                request_id,
                "This interactive request type is not supported yet.",
            )
        return False

    def answer_user_input_request(
        self, request_id: int, params: Dict[str, Any]
    ) -> None:
        dialog = Gtk.Dialog(
            title="Codex needs your input",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Send answer", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(12)
        area.set_border_width(16)
        widgets: List[tuple[str, Any, Optional[Gtk.Entry]]] = []
        for question in (params.get("questions") or [])[:3]:
            question_id = str(question.get("id") or len(widgets))
            title = Gtk.Label()
            title.set_markup(
                f"<b>{GLib.markup_escape_text(str(question.get('header') or 'Question'))}</b>\n"
                f"{GLib.markup_escape_text(str(question.get('question') or ''))}"
            )
            question_text = str(question.get("question") or "")
            rtl_question = content_prefers_rtl(question_text)
            title.set_xalign(1.0 if rtl_question else 0.0)
            title.set_line_wrap(True)
            title.set_direction(
                Gtk.TextDirection.RTL if rtl_question else Gtk.TextDirection.LTR
            )
            area.pack_start(title, False, False, 0)
            options = question.get("options") or []
            other_entry: Optional[Gtk.Entry] = None
            if options:
                control = Gtk.ComboBoxText()
                for option in options:
                    label = str(option.get("label") or "")
                    description = str(option.get("description") or "")
                    control.append(label, f"{label} — {description}" if description else label)
                control.set_active(0)
            else:
                control = Gtk.Entry()
                control.set_visibility(not bool(question.get("isSecret")))
                control.set_placeholder_text("Your answer")
                control.set_activates_default(True)
            if question.get("isOther"):
                other_entry = Gtk.Entry()
                other_entry.set_visibility(not bool(question.get("isSecret")))
                other_entry.set_placeholder_text("Or write another answer")
                area.pack_start(other_entry, False, False, 0)
            area.pack_start(control, False, False, 0)
            widgets.append((question_id, control, other_entry))
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        answers: Dict[str, Dict[str, List[str]]] = {}
        if response == Gtk.ResponseType.OK:
            for question_id, control, other_entry in widgets:
                custom = other_entry.get_text().strip() if other_entry else ""
                if custom:
                    values = [custom]
                elif isinstance(control, Gtk.ComboBoxText):
                    values = [str(control.get_active_id() or "")]
                else:
                    values = [control.get_text().strip()]
                answers[question_id] = {"answers": [value for value in values if value]}
        dialog.destroy()
        self.bridge.respond(request_id, {"answers": answers})

    def answer_mcp_elicitation(
        self, request_id: int, params: Dict[str, Any]
    ) -> None:
        message = str(params.get("message") or "A connected tool needs more information.")
        mode = str(params.get("mode") or "form")
        if mode == "url" and params.get("url"):
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text="Open the tool page?",
            )
            dialog.format_secondary_text(message)
            dialog.add_button("Decline", Gtk.ResponseType.CANCEL)
            dialog.add_button("Open", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.OK:
                Gtk.show_uri_on_window(self, str(params["url"]), Gdk.CURRENT_TIME)
                self.bridge.respond(request_id, {"action": "accept"})
            else:
                self.bridge.respond(request_id, {"action": "decline"})
            return

        schema = params.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        dialog = Gtk.Dialog(
            title="Connected tool request",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_button("Decline", Gtk.ResponseType.CANCEL)
        dialog.add_button("Approve", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(10)
        area.set_border_width(16)
        description = Gtk.Label(label=message)
        description.set_xalign(1.0)
        description.set_line_wrap(True)
        area.pack_start(description, False, False, 0)
        controls: Dict[str, Any] = {}
        for key, definition in properties.items():
            label = Gtk.Label(label=str(definition.get("title") or key))
            label.set_xalign(1.0)
            area.pack_start(label, False, False, 0)
            values = definition.get("enum")
            if values:
                control = Gtk.ComboBoxText()
                names = definition.get("enumNames") or values
                for value, name in zip(values, names):
                    control.append(str(value), str(name))
                control.set_active_id(str(definition.get("default") or values[0]))
            elif definition.get("type") == "boolean":
                control = Gtk.CheckButton()
                control.set_active(bool(definition.get("default")))
            else:
                control = Gtk.Entry()
                default = definition.get("default")
                if default is not None:
                    control.set_text(str(default))
            controls[str(key)] = control
            area.pack_start(control, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        content: Dict[str, Any] = {}
        if response == Gtk.ResponseType.OK:
            for key, control in controls.items():
                if isinstance(control, Gtk.ComboBoxText):
                    content[key] = control.get_active_id()
                elif isinstance(control, Gtk.CheckButton):
                    content[key] = control.get_active()
                else:
                    content[key] = control.get_text()
        dialog.destroy()
        self.bridge.respond(
            request_id,
            {"action": "accept", "content": content}
            if response == Gtk.ResponseType.OK
            else {"action": "decline"},
        )

    def _cancel_final_answer_fallback(self, state: Dict[str, Any]) -> None:
        source_id = state.get("final_answer_source_id")
        if source_id is not None:
            GLib.source_remove(source_id)
        state["final_answer_source_id"] = None
        state["final_answer_generation"] = (
            int(state.get("final_answer_generation") or 0) + 1
        )

    def _schedule_final_answer_fallback(self, room_id: int) -> None:
        state = self.room_turns.get(room_id)
        if (
            not state
            or not state.get("final_text")
            or state.get("final_answer_source_id") is not None
        ):
            return
        generation = int(state.get("final_answer_generation") or 0) + 1
        state["final_answer_generation"] = generation

        def finish_if_terminal() -> bool:
            current = self.room_turns.get(room_id)
            if current is not state or current.get("final_answer_generation") != generation:
                return False
            current["final_answer_source_id"] = None
            self.finish_turn(room_id)
            return False

        state["final_answer_source_id"] = GLib.timeout_add(
            FINAL_ANSWER_GRACE_MILLISECONDS,
            finish_if_terminal,
        )

    def _room_turn_error(self, room_id: int, error: Dict[str, Any]) -> bool:
        if error.get("transient"):
            state = self.room_turns.get(room_id)
            if state:
                self._mark_turn_reconnecting(
                    state,
                    "The response is waiting for Codex to reconnect.",
                )
                self.refresh_rooms()
            return False
        self.finish_turn(room_id, error_text=error.get("message") or str(error))
        return False

    def finish_turn(self, room_id: int, error_text: Optional[str] = None) -> None:
        state = self.room_turns.get(room_id)
        if not state:
            return
        self._cancel_final_answer_fallback(state)
        answer = inflight_turn_display_text(state)
        if error_text and not answer:
            answer = f"Error: {error_text}"
        if not answer:
            answer = (
                "Response stopped."
                if state["stop_requested"]
                else "No response was received. Please try again."
            )
        duration = max(0.0, time.monotonic() - state["started_at"])
        source_id = state.get("render_source_id")
        if source_id is not None:
            GLib.source_remove(source_id)
            state["render_source_id"] = None
        follow = bool(
            self.active_room
            and int(self.active_room["id"]) == room_id
            and self.is_near_bottom()
        )
        if state.get("bubble"):
            state["bubble"].set_text(answer)
            state["bubble"].hide_timer(duration)
        message_id = self.store.add_message(
            room_id,
            "assistant",
            answer,
            duration_seconds=duration,
            token_usage=state.get("token_usage"),
        )
        if state.get("bubble"):
            state["bubble"].message_id = message_id
            if hasattr(self, "message_bubbles"):
                self.message_bubbles[message_id] = state["bubble"]
            if hasattr(state["bubble"], "_selection_changed"):
                state["bubble"]._selection_changed(
                    state["bubble"].content_view,
                    state["bubble"].content_view.get_selected_text(),
                )
        active_id = int(self.active_room["id"]) if self.active_room else None
        if active_id != room_id:
            self.unread_rooms.add(room_id)
        else:
            self.store.mark_room_read(room_id)
        self.room_turns.pop(room_id, None)
        self.sync_active_room_controls()
        self.refresh_rooms()
        if active_id == room_id:
            if follow:
                self.scroll_to_bottom(force=False)
            else:
                self.on_scroll_position_changed()

    def sync_active_room_controls(self) -> None:
        room_id = int(self.active_room["id"]) if self.active_room else None
        state = self.room_turns.get(room_id) if room_id is not None else None
        idle = state is None
        has_content = bool(self._input_text().strip() or self.pending_image)
        self.send_button.set_sensitive(
            has_content
            and self.connected
            and bool(self.models)
            and (idle or bool(state and state.get("turn_id")))
        )
        self.send_button.set_tooltip_text(
            "Add guidance to the current response"
            if state
            else "Send message (press Enter twice)"
        )
        self.stop_button.set_sensitive(bool(state) and not state.get("stop_requested"))
        self.stop_button.set_no_show_all(idle)
        if idle:
            self.stop_button.hide()
        else:
            self.stop_button.show()
        self.image_button.set_sensitive(True)
        # These selectors configure only the active room.  Turns running in
        # other rooms must not make the active idle room read-only.
        self.model_combo.set_sensitive(
            self.active_room is not None
            and bool(self.models)
            and not room_is_busy(self.active_room, self.room_turns)
        )
        self.update_model_tooltip()
        self.update_effort_options()
        # Let the user prepare the next prompt while this room is streaming.
        self.input_view.set_editable(True)
        if self.connected:
            active_count = len(self.room_turns)
            if active_count:
                self.set_status(f"{active_count} active task{'s' if active_count != 1 else ''}", "working")
            else:
                self.set_status("Connected", "online")

    def _update_turn_timers(self) -> bool:
        now = time.monotonic()
        expire_disconnected = getattr(self, "_expire_disconnected_turns", None)
        if callable(expire_disconnected):
            expire_disconnected(now)
        for room_id, state in list(self.room_turns.items()):
            elapsed = int(now - state["started_at"])
            bubble = state.get("bubble")
            if bubble:
                bubble.set_elapsed(
                    elapsed,
                    bool(state.get("stop_requested")),
                )
            button = self.room_buttons.get(room_id)
            if button:
                presentation = str(state.get("activity_presentation") or "working")
                if state.get("stop_requested") or presentation == "stopping":
                    status = "Stopping"
                elif presentation == "reconnecting":
                    status = "Reconnecting"
                elif presentation == "restoring":
                    status = "Restoring"
                else:
                    status = "Responding"
                text = f"● {status} • {format_duration(elapsed)}"
                if hasattr(button, "set_status"):
                    button.set_status(text)
                else:  # Compatibility for controller fakes in headless tests.
                    button.set_label(
                        f"{self.room_titles.get(room_id, 'Chat')}\n{text}"
                    )
        return True

    def set_status(self, text: str, style: str) -> None:
        self.status_label.set_text("●")
        self.status_label.set_tooltip_text(text)
        self.status_label.get_accessible().set_name(f"Codex status: {text}")
        context = self.status_label.get_style_context()
        for name in ("online", "connecting", "working", "warning", "error"):
            context.remove_class(name)
        context.add_class(style)

    def on_manual_scroll(self, *_args: Any) -> bool:
        self._manual_scroll_serial += 1
        serial = self._manual_scroll_serial

        def maybe_load_history() -> bool:
            if serial != self._manual_scroll_serial:
                return False
            self.on_scroll_position_changed()
            return False

        GLib.idle_add(maybe_load_history)
        return False

    def scroll_to_bottom(
        self, force: bool = True, generation: Optional[int] = None
    ) -> None:
        serial = self._manual_scroll_serial

        def do_scroll() -> bool:
            if getattr(self, "_closing", False) or not hasattr(self, "message_scroll"):
                return False
            if generation is not None and generation != self._room_load_generation:
                return False
            if not force and serial != self._manual_scroll_serial:
                return False
            adjustment = self.message_scroll.get_vadjustment()
            if adjustment is None:
                return False
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
            return False

        GLib.idle_add(do_scroll)

    def is_near_bottom(self, threshold: float = 72.0) -> bool:
        adjustment = self.message_scroll.get_vadjustment()
        distance = adjustment.get_upper() - (
            adjustment.get_value() + adjustment.get_page_size()
        )
        return distance <= threshold

    def show_error(self, title: str, message: str) -> None:
        if not hasattr(self, "error_bar"):
            return
        self.error_label.set_text(f"{title} — {message}")
        self.error_bar.set_no_show_all(False)
        self.error_bar.show_all()
        if self._error_hide_source_id is not None:
            GLib.source_remove(self._error_hide_source_id)

        def hide() -> bool:
            self._error_hide_source_id = None
            self.error_bar.hide()
            return False

        self._error_hide_source_id = GLib.timeout_add_seconds(9, hide)

    def _active_turn_states(self) -> List[Dict[str, Any]]:
        """Return turns that can still produce work after the window closes."""
        return [
            state
            for state in self.room_turns.values()
            if not state.get("stop_requested")
        ]

    def _finish_close(self, stop_responses: bool = False) -> None:
        if self._closing:
            return
        if stop_responses:
            for state in self._active_turn_states():
                state["stop_requested"] = True
                thread_id = state.get("thread_id")
                turn_id = state.get("turn_id")
                if thread_id and turn_id:
                    self.bridge.interrupt(str(thread_id), str(turn_id))
        self._closing = True
        self.save_active_draft()
        self.save_active_view_state()
        if self._draft_save_source_id is not None:
            GLib.source_remove(self._draft_save_source_id)
            self._draft_save_source_id = None
        if self._enter_send_hint_source_id is not None:
            GLib.source_remove(self._enter_send_hint_source_id)
            self._enter_send_hint_source_id = None
        if self._composer_notice_source_id is not None:
            GLib.source_remove(self._composer_notice_source_id)
            self._composer_notice_source_id = None
        if self._composer_direction_idle_source_id is not None:
            GLib.source_remove(self._composer_direction_idle_source_id)
            self._composer_direction_idle_source_id = None
        if hasattr(self, "input_view"):
            text_buffer = self.input_view.get_buffer()
            for job in self._composer_direction_jobs:
                self._discard_composer_direction_job(text_buffer, job)
            self._composer_direction_jobs.clear()
        GLib.source_remove(self._timer_source_id)
        if self._error_hide_source_id is not None:
            GLib.source_remove(self._error_hide_source_id)
            self._error_hide_source_id = None
        if self._reconnect_source_id is not None:
            GLib.source_remove(self._reconnect_source_id)
            self._reconnect_source_id = None
        for state in self.room_turns.values():
            source_id = state.get("render_source_id")
            if source_id is not None:
                GLib.source_remove(source_id)
                state["render_source_id"] = None
            self._cancel_final_answer_fallback(state)
        # Closing the UI socket deliberately leaves the persistent backend
        # alive.  It buffers events and continues turns for the next launch.
        self.bridge.close()
        self.store.close()

    def _on_close(self, *_args: Any) -> bool:
        if self._closing:
            return False
        active = self._active_turn_states()
        if not active:
            self._finish_close(False)
            return False
        if self._close_dialog is not None:
            return True

        dialog = Gtk.Dialog(
            title="Close Codex",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        self._close_dialog = dialog
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Continue in background", Gtk.ResponseType.APPLY)
        dialog.add_button("Stop responses and close", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(10)
        area.set_border_width(18)
        message = Gtk.Label(
            label=(
                f"{len(active)} response{'s' if len(active) != 1 else ''} "
                "is still running. What should happen when Codex closes?"
            )
        )
        message.set_line_wrap(True)
        message.set_xalign(0.0)
        area.pack_start(message, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        self._close_dialog = None
        if response == Gtk.ResponseType.CANCEL:
            return True
        self._finish_close(response == Gtk.ResponseType.OK)
        return False


class CodexApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window: Optional[MainWindow] = None

    def do_activate(self) -> None:
        if not self.window:
            self.window = MainWindow(self)
        self.window.show_all()
        self.window.present()


def self_check() -> int:
    errors: List[str] = []
    if not CodexBridge.find_codex():
        errors.append("Codex was not found in PATH or ~/.local/bin")
    if not ICON_PATH.exists():
        errors.append("The app icon was not found")
    try:
        store = Store(Path(os.environ.get("TMPDIR", "/tmp")) / "codex-check")
        room = store.create_room("Storage check")
        store.add_message(int(room["id"]), "user", "UTF-8 storage is ready")
        store.close()
    except Exception as exc:  # pragma: no cover - diagnostic path
        errors.append(f"Storage: {exc}")
    if errors:
        print("CHECK FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("CHECK OK — GTK, UTF-8, Codex CLI, storage and icon are ready")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--check", action="store_true", help="Run checks without opening the window")
    args = parser.parse_args()
    if args.check:
        return self_check()
    Gtk.Widget.set_default_direction(Gtk.TextDirection.LTR)
    application = CodexApplication()
    return application.run(sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
