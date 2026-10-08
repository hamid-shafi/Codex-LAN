# -*- coding: utf-8 -*-
"""Safe, GTK-native Markdown rendering for chat messages.

This is deliberately a small renderer rather than an HTML/WebKit view. It
supports the Markdown people routinely use in chat while keeping all content
escaped before it reaches Pango markup.
"""

from __future__ import annotations

import html
import re
import unicodedata
import weakref
from pathlib import PurePosixPath
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GtkSource", "4")
    from gi.repository import GtkSource  # type: ignore  # noqa: E402
except (ImportError, ValueError):  # pragma: no cover - depends on Linux image
    GtkSource = None


FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
TECHNICAL_RUN_RE = re.compile(
    r"(?<!\w)"
    r"([A-Za-z0-9][A-Za-z0-9_./:@#+=<>%&?-]*"
    r"(?:[ \t]+[A-Za-z0-9][A-Za-z0-9_./:@#+=<>%&?-]*)*)"
    r"(?!\w)"
)
BARE_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
INLINE_CODE_DIRECTION_RE = re.compile(r"`+[^`\n]*`+")
MARKDOWN_LINK_DESTINATION_RE = re.compile(r"\]\([^\n)]*\)")
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
MAX_INLINE_NESTING = 8
MAX_LANGUAGE_LENGTH = 32
SELECTION_BLUE = (0x35 * 257, 0x84 * 257, 0xE4 * 257)
SELECTION_WHITE = (0xFF * 257, 0xFF * 257, 0xFF * 257)
CODE_LINE_HEIGHT = 20
CODE_VERTICAL_PADDING = 18


class _SelectionSnapshot(NamedTuple):
    start: int
    end: int
    text: str


def _xml_safe(value: str) -> str:
    """Remove characters rejected by Pango's XML-compatible markup parser."""
    value = CONTROL_RE.sub("�", value)
    return "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)


def is_rtl(text: str) -> bool:
    """Return the direction of the first strong character in *text*."""
    for character in text or "":
        direction = unicodedata.bidirectional(character)
        if direction in {"R", "AL"}:
            return True
        if direction == "L":
            return False
    return False


def _directional_word_lengths(text: str) -> Tuple[List[int], List[int]]:
    """Count lexical runs by Unicode direction, not a particular script block.

    Combining marks and joiners stay inside their word so accented or joined
    spelling does not manufacture extra votes for either reading direction.
    """
    rtl_words: List[int] = []
    ltr_words: List[int] = []
    previous = ""
    for character in text:
        direction = unicodedata.bidirectional(character)
        side = ""
        if character.isalpha():
            if direction in {"R", "AL"}:
                side = "rtl"
            elif direction == "L":
                side = "ltr"
        if side:
            words = rtl_words if side == "rtl" else ltr_words
            if side == previous:
                words[-1] += 1
            else:
                words.append(1)
            previous = side
        elif (
            character in {"\u200c", "\u200d"}
            or unicodedata.category(character).startswith("M")
        ):
            continue
        else:
            previous = ""
    return rtl_words, ltr_words


def content_prefers_rtl(text: str) -> bool:
    """Choose a stable reading direction for a mixed-language prose block.

    First-strong direction is right for monolingual text, but chat messages
    frequently start with a Latin product/command name and then continue in
    an RTL language. Treat that as RTL only when the continuation has at
    least two lexical runs and is substantial relative to the LTR prose.
    This keeps an otherwise LTR sentence containing one RTL term LTR.

    Inline code, link destinations, and bare URLs are excluded from the vote:
    their technical spelling must not decide the surrounding prose direction.
    Code blocks and tables deliberately continue to use ``is_rtl`` directly.
    """
    sample = INLINE_CODE_DIRECTION_RE.sub(" ", text or "")
    sample = MARKDOWN_LINK_DESTINATION_RE.sub("]", sample)
    sample = BARE_URL_RE.sub(" ", sample)
    if is_rtl(sample):
        return True

    rtl_words, ltr_words = _directional_word_lengths(sample)
    if len(rtl_words) < 2:
        return False
    if not ltr_words:
        return True

    # RTL text supplying at least half of the lexical runs establishes the
    # paragraph frame. For a fragmented technical prefix, require three RTL
    # runs and at least half as many RTL letters as LTR letters.
    if len(rtl_words) >= len(ltr_words):
        return True
    rtl_letters = sum(rtl_words)
    ltr_letters = sum(ltr_words)
    return len(rtl_words) >= 3 and rtl_letters * 2 >= ltr_letters


def _local_file_reference(
    destination: str,
) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Parse an absolute local path with optional ``:line[:column]``."""
    if (
        not destination
        or len(destination) > 4096
        or not destination.startswith("/")
        or CONTROL_RE.search(destination)
        or BIDI_CONTROL_RE.search(destination)
        or any(char in "\t\r\n" for char in destination)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in destination)
    ):
        return None
    match = re.fullmatch(
        r"(?P<path>/.*?)(?::(?P<line>[1-9]\d*)(?::(?P<column>[1-9]\d*))?)?",
        destination,
    )
    if match is None:
        return None
    path = match.group("path")
    basename = PurePosixPath(path).name
    if path.endswith("/") or not basename or basename in {".", ".."}:
        return None
    line = int(match.group("line")) if match.group("line") else None
    column = int(match.group("column")) if match.group("column") else None
    return path, line, column


def _local_file_label_markup(path: str, rtl_context: bool) -> str:
    """Render a basename literally and keep it together inside RTL prose."""
    basename = html.escape(_xml_safe(PurePosixPath(path).name))
    return f"\u2066{basename}\u2069" if rtl_context else basename


def _is_safe_web_link(url: str) -> bool:
    """Allow only ordinary absolute HTTP(S) links with no control characters."""
    if not url or len(url) > 4096 or any(char.isspace() for char in url):
        return False
    if CONTROL_RE.search(url) or any(0xD800 <= ord(char) <= 0xDFFF for char in url):
        return False
    try:
        parsed = urlsplit(url)
        _ = parsed.port  # Also rejects malformed port values.
    except (ValueError, UnicodeError):
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _is_safe_link(url: str) -> bool:
    """Allow safe web destinations and absolute local file references."""
    return _is_safe_web_link(url) or _local_file_reference(url) is not None


def _plain_markup(text: str, rtl_context: bool) -> str:
    """Escape plain text and isolate technical LTR runs in RTL prose."""
    text = _xml_safe(text)
    if not rtl_context:
        return html.escape(text)
    pieces: List[str] = []
    cursor = 0
    for match in TECHNICAL_RUN_RE.finditer(text):
        pieces.append(html.escape(text[cursor : match.start()]))
        pieces.append("\u2066" + html.escape(match.group(1)) + "\u2069")
        cursor = match.end()
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces)


def _find_run_end(text: str, start: int, marker: str) -> int:
    """Find an unescaped matching inline delimiter."""
    cursor = start
    while True:
        cursor = text.find(marker, cursor)
        if cursor < 0:
            return -1
        backslashes = 0
        check = cursor - 1
        while check >= 0 and text[check] == "\\":
            backslashes += 1
            check -= 1
        if backslashes % 2 == 0:
            return cursor
        cursor += len(marker)


def _find_closing_bracket(text: str, start: int) -> int:
    depth = 0
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            if depth == 0:
                return cursor
            depth -= 1
        cursor += 1
    return -1


def _parenthesized_destination(text: str, opening: int) -> Optional[Tuple[str, int]]:
    """Return ``(URL, end_index)`` for a Markdown link destination."""
    depth = 1
    cursor = opening + 1
    end = -1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = cursor
                break
        cursor += 1
    if end < 0:
        return None
    body = text[opening + 1 : end].strip()
    if not body:
        return None
    if body.startswith("<"):
        angle_end = body.find(">", 1)
        if angle_end < 0:
            return None
        destination = body[1:angle_end]
    else:
        destination = re.split(r"\s+", body, maxsplit=1)[0]
    destination = re.sub(r"\\([\\`()_*{}\[\]<>#+.!~-])", r"\1", destination)
    return destination, end + 1


def _trim_bare_url(url: str) -> str:
    """Drop sentence punctuation without eating balanced URL parentheses."""
    while url and url[-1] in ".,!?:;،؛":
        url = url[:-1]
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    while url and url[-1] in "]}":
        url = url[:-1]
    return url


def inline_markup(
    text: str,
    rtl_context: Optional[bool] = None,
    _depth: int = 0,
    _links_allowed: bool = True,
) -> str:
    """Convert a conservative inline Markdown subset to safe Pango markup."""
    text = text or ""
    if rtl_context is None:
        rtl_context = is_rtl(text)
    if _depth >= MAX_INLINE_NESTING:
        return _plain_markup(text, rtl_context)

    pieces: List[str] = []
    plain: List[str] = []

    def flush_plain() -> None:
        if plain:
            pieces.append(_plain_markup("".join(plain), bool(rtl_context)))
            plain.clear()

    cursor = 0
    while cursor < len(text):
        char = text[cursor]

        if char == "\\" and cursor + 1 < len(text) and text[cursor + 1] in r"\\`*_{}[]()#+-.!~|>":
            plain.append(text[cursor + 1])
            cursor += 2
            continue

        if char == "`":
            run_end = cursor + 1
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            marker = text[cursor:run_end]
            closing = _find_run_end(text, run_end, marker)
            if closing >= 0:
                flush_plain()
                code = text[run_end:closing].replace("\n", " ")
                if len(code) >= 2 and code.startswith(" ") and code.endswith(" ") and code.strip():
                    code = code[1:-1]
                pieces.append(
                    '<span font_family="monospace" background="#00000022">\u2066'
                    + html.escape(_xml_safe(code))
                    + "\u2069</span>"
                )
                cursor = closing + len(marker)
                continue

        image = char == "!" and cursor + 1 < len(text) and text[cursor + 1] == "["
        link_start = cursor + 1 if image else cursor
        if _links_allowed and text[link_start : link_start + 1] == "[":
            closing_label = _find_closing_bracket(text, link_start + 1)
            if (
                closing_label >= 0
                and closing_label + 1 < len(text)
                and text[closing_label + 1] == "("
            ):
                parsed = _parenthesized_destination(text, closing_label + 1)
                if parsed is not None:
                    url, end = parsed
                    if _is_safe_link(url):
                        flush_plain()
                        local_reference = _local_file_reference(url)
                        if local_reference is None:
                            label_text = text[link_start + 1 : closing_label] or url
                            label_markup = inline_markup(
                                label_text,
                                rtl_context=rtl_context,
                                _depth=_depth + 1,
                                _links_allowed=False,
                            )
                        else:
                            label_markup = _local_file_label_markup(
                                local_reference[0], bool(rtl_context)
                            )
                        if image:
                            label_markup = "Image: " + label_markup
                        href = html.escape(_xml_safe(url), quote=True)
                        pieces.append(f'<a href="{href}">{label_markup}</a>')
                        cursor = end
                        continue

        if _links_allowed and char == "<":
            closing = text.find(">", cursor + 1)
            if closing >= 0:
                url = text[cursor + 1 : closing]
                if _is_safe_link(url):
                    flush_plain()
                    local_reference = _local_file_reference(url)
                    if local_reference is None:
                        display_markup = "\u2066" + html.escape(_xml_safe(url)) + "\u2069"
                    else:
                        display_markup = _local_file_label_markup(
                            local_reference[0], bool(rtl_context)
                        )
                    pieces.append(
                        f'<a href="{html.escape(_xml_safe(url), quote=True)}">'
                        f"{display_markup}</a>"
                    )
                    cursor = closing + 1
                    continue

        inside_incomplete_markdown_link = (
            cursor >= 2 and text[cursor - 2 : cursor] == "]("
        )
        if _links_allowed and not inside_incomplete_markdown_link and (
            text[cursor : cursor + 7].lower() == "http://"
            or text[cursor : cursor + 8].lower() == "https://"
        ):
            match = BARE_URL_RE.match(text, cursor)
            if match:
                url = _trim_bare_url(match.group(0))
                if _is_safe_link(url):
                    flush_plain()
                    escaped_url = html.escape(_xml_safe(url))
                    pieces.append(
                        f'<a href="{html.escape(_xml_safe(url), quote=True)}">'
                        f"\u2066{escaped_url}\u2069</a>"
                    )
                    cursor += len(url)
                    continue

        marker = ""
        tag = ""
        if text.startswith("**", cursor) or text.startswith("__", cursor):
            marker, tag = text[cursor : cursor + 2], "b"
        elif text.startswith("~~", cursor):
            marker, tag = "~~", "s"
        elif char in "*_":
            if char == "_" and cursor > 0 and text[cursor - 1].isalnum():
                marker = ""
            else:
                marker, tag = char, "i"
        if marker:
            closing = _find_run_end(text, cursor + len(marker), marker)
            # In ``**bold *italic***`` the first star in the closing run ends
            # the nested emphasis and the final two end the strong span.
            if closing >= 0 and len(marker) == 2:
                run_end = closing
                while run_end < len(text) and text[run_end] == marker[0]:
                    run_end += 1
                if run_end - closing > len(marker):
                    closing = run_end - len(marker)
            inner = text[cursor + len(marker) : closing] if closing >= 0 else ""
            if closing >= 0 and inner and not inner.isspace():
                if marker == "_" and closing + 1 < len(text) and text[closing + 1].isalnum():
                    closing = -1
                if closing >= 0:
                    flush_plain()
                    pieces.append(
                        f"<{tag}>"
                        + inline_markup(
                            inner,
                            rtl_context=rtl_context,
                            _depth=_depth + 1,
                            _links_allowed=_links_allowed,
                        )
                        + f"</{tag}>"
                    )
                    cursor = closing + len(marker)
                    continue

        plain.append(char)
        cursor += 1

    flush_plain()
    return "".join(pieces)


def _language_name(info: str) -> str:
    stripped = (info or "").strip()
    token = stripped.split(maxsplit=1)[0] if stripped else ""
    token = token.lstrip(".")[:MAX_LANGUAGE_LENGTH]
    return token if re.fullmatch(r"[A-Za-z0-9_+.#-]+", token or "") else ""


def _split_table_row(line: str) -> List[str]:
    """Split a Markdown table row, preserving escaped and code-span pipes."""
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    cells: List[str] = []
    cell: List[str] = []
    code_ticks = 0
    cursor = 0
    while cursor < len(value):
        char = value[cursor]
        if char == "\\" and cursor + 1 < len(value):
            cell.extend((char, value[cursor + 1]))
            cursor += 2
            continue
        if char == "`":
            run_end = cursor + 1
            while run_end < len(value) and value[run_end] == "`":
                run_end += 1
            run_length = run_end - cursor
            if code_ticks == 0:
                code_ticks = run_length
            elif code_ticks == run_length:
                code_ticks = 0
            cell.append(value[cursor:run_end])
            cursor = run_end
            continue
        if char == "|" and code_ticks == 0:
            cells.append("".join(cell).strip())
            cell.clear()
        else:
            cell.append(char)
        cursor += 1
    cells.append("".join(cell).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def split_blocks(markdown: str) -> List[Tuple[str, str, str]]:
    """Return ``(kind, language, content)`` blocks without interpreting HTML."""
    blocks: List[Tuple[str, str, str]] = []
    prose: List[str] = []
    code: List[str] = []
    language = ""
    fence_character = ""
    fence_length = 0
    fenced = False

    def flush_prose() -> None:
        if prose:
            blocks.append(("prose", "", "\n".join(prose)))
            prose.clear()

    normalized = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        fence = FENCE_RE.match(line)
        if fenced:
            if (
                fence
                and fence.group("fence")[0] == fence_character
                and len(fence.group("fence")) >= fence_length
                and not fence.group("info").strip()
            ):
                blocks.append(("code", language, "\n".join(code)))
                code.clear()
                fenced = False
                language = ""
                continue
            code.append(line)
            continue
        if fence:
            flush_prose()
            marker = fence.group("fence")
            fence_character = marker[0]
            fence_length = len(marker)
            language = _language_name(fence.group("info"))
            fenced = True
            continue
        prose.append(line)
    if fenced:
        blocks.append(("code", language, "\n".join(code)))
    else:
        flush_prose()

    expanded: List[Tuple[str, str, str]] = []
    for kind, block_language, content in blocks:
        if kind != "prose":
            expanded.append((kind, block_language, content))
            continue
        lines = content.splitlines()
        prose_lines: List[str] = []

        def flush_lines() -> None:
            if prose_lines:
                expanded.append(("prose", "", "\n".join(prose_lines)))
                prose_lines.clear()

        index = 0
        while index < len(lines):
            if (
                index + 1 < len(lines)
                and "|" in lines[index]
                and _is_table_separator(lines[index + 1])
                and len(_split_table_row(lines[index]))
                == len(_split_table_row(lines[index + 1]))
            ):
                flush_lines()
                rows = [lines[index]]
                column_count = len(_split_table_row(lines[index]))
                index += 2
                while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                    if len(_split_table_row(lines[index])) != column_count:
                        break
                    rows.append(lines[index])
                    index += 1
                expanded.append(("table", "", "\n".join(rows)))
                continue
            prose_lines.append(lines[index])
            index += 1
        flush_lines()
    return expanded


def _message_is_rtl(blocks: Sequence[Tuple[str, str, str]], prefer_rtl: bool) -> bool:
    """Keep a stable message summary direction for compatibility/streaming."""
    sample = "\n".join(content for kind, _language, content in blocks if kind != "code")
    sample = re.sub(r"`+[^`]*`+", " ", sample)
    sample = BARE_URL_RE.sub(" ", sample)
    rtl_runs, ltr_runs = _directional_word_lengths(sample)
    if not rtl_runs:
        return prefer_rtl and not ltr_runs
    if not ltr_runs:
        return True
    if is_rtl(sample):
        return True
    if len(rtl_runs) >= 2 and sum(rtl_runs) >= 4:
        return True
    return len(rtl_runs) > len(ltr_runs)


def _stable_prefix_length(markdown: str) -> int:
    """Return an append-safe boundary for incremental streaming renders.

    A completed blank-line-delimited block cannot be changed by future text.
    A closed fenced block is stable once its closing line has a newline.  The
    unfinished tail remains replaceable, so partially streamed Markdown still
    reflows correctly as delimiters arrive.
    """
    offset = 0
    stable = 0
    fenced = False
    fence_character = ""
    fence_length = 0
    normalized = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.splitlines(keepends=True):
        has_newline = raw_line.endswith("\n")
        line = raw_line[:-1] if has_newline else raw_line
        fence = FENCE_RE.match(line)
        offset += len(raw_line)
        if fenced:
            if (
                fence
                and fence.group("fence")[0] == fence_character
                and len(fence.group("fence")) >= fence_length
                and not fence.group("info").strip()
            ):
                fenced = False
                if has_newline:
                    stable = offset
            continue
        if fence:
            marker = fence.group("fence")
            fence_character = marker[0]
            fence_length = len(marker)
            fenced = True
        elif not line.strip() and has_newline:
            stable = offset
    return stable


class MarkdownView(Gtk.Box):
    _native_selection_css: Optional[Gtk.CssProvider] = None
    _native_selection_owner: Optional[weakref.ReferenceType["MarkdownView"]] = None

    __gsignals__ = {
        "selection-changed": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str,),
        ),
        # Emitted instead of GTK's per-widget context menu.  Root coordinates
        # let the message bubble place one menu for selections that span
        # several labels and code views.
        "selection-context-requested": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (
                str,
                GObject.TYPE_PYOBJECT,
                GObject.TYPE_OBJECT,
                GObject.TYPE_DOUBLE,
                GObject.TYPE_DOUBLE,
            ),
        ),
    }

    def __init__(self, text: str = "", user_message: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._install_native_selection_css()
        self.user_message = user_message
        self.message_rtl = False
        self._stable_text = ""
        self._rendered_text: Optional[str] = None
        self._tail_widgets: List[Gtk.Widget] = []
        self._selection_widgets: List[Gtk.Widget] = []
        self._remembered_selections: Dict[Gtk.Widget, _SelectionSnapshot] = {}
        self._native_selection_fallbacks: Dict[
            Gtk.Widget, _SelectionSnapshot
        ] = {}
        # Keep the owner beside each synthetic selection tag. GtkSourceView
        # may replace its buffer while the widget is being destroyed; an old
        # tag must never be applied to that replacement buffer.
        self._selection_tags: Dict[
            Gtk.TextView, Tuple[Gtk.TextBuffer, Gtk.TextTag]
        ] = {}
        # GtkSource color schemes carry syntax foreground colors, so CSS alone
        # cannot make a dark editor look native inside the light application
        # theme. Track live code views and swap their complete scheme whenever
        # the renderer moves under a differently themed window.
        self._source_buffers: Dict[Gtk.TextView, Gtk.TextBuffer] = {}
        self._source_scheme_id = ""
        self._code_theme: Optional[str] = None
        self._selection_emit_source: Optional[int] = None
        self._clearing_selection = False
        self._active_selection_widget: Optional[Gtk.Widget] = None
        self._native_selection_cache: Optional[
            Tuple[weakref.ReferenceType[Gtk.Widget], _SelectionSnapshot]
        ] = None
        self._native_selection_cache_armed = False
        self._native_selection_recovery_source: Optional[int] = None
        self._native_selection_verify_source: Optional[int] = None
        self._native_selection_recovering = False
        self._drag_anchor: Optional[Tuple[Gtk.Widget, int]] = None
        self._drag_aggregate = False
        self._drag_pointer_root: Optional[Tuple[float, float]] = None
        self._drag_scroll_source: Optional[int] = None
        self._document_selection_active = False
        self._manual_prose_gesture = False
        self._manual_prose_preserve_existing = False
        self._manual_prose_preserved: Dict[Gtk.Widget, _SelectionSnapshot] = {}
        self._plain_pointer_active = False
        self._plain_release_source: Optional[int] = None
        self.connect("hierarchy-changed", self._sync_source_color_scheme)
        self.connect("style-updated", self._sync_source_color_scheme)
        self.set_text(text)

    @classmethod
    def _install_native_selection_css(cls) -> None:
        if cls._native_selection_css is not None:
            return
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"label.markdown-prose { caret-color: transparent; }"
        )
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
        )
        cls._native_selection_css = provider

    def _application_theme(self) -> str:
        if self._code_theme in {"light", "night", "graphite"}:
            return self._code_theme
        widget: Optional[Gtk.Widget] = self
        while widget is not None:
            context = widget.get_style_context()
            if context.has_class("theme-light"):
                return "light"
            if context.has_class("theme-graphite"):
                return "graphite"
            if context.has_class("theme-night"):
                return "night"
            widget = widget.get_parent()
        return "night"

    def set_code_theme(self, theme: str) -> None:
        """Apply the app theme to existing and future fenced code blocks."""
        self._code_theme = theme if theme in {"light", "night", "graphite"} else None
        self._sync_source_color_scheme()

    def _sync_source_color_scheme(self, *_args: object) -> None:
        """Match fenced-code syntax colors to the active application theme."""
        if GtkSource is None or not self._source_buffers:
            return
        theme = self._application_theme()
        candidates = (
            ("Yaru", "solarized-light", "classic")
            if theme == "light"
            else ("Yaru-dark", "solarized-dark", "oblivion")
        )
        manager = GtkSource.StyleSchemeManager.get_default()
        scheme = next(
            (manager.get_scheme(name) for name in candidates if manager.get_scheme(name)),
            None,
        )
        scheme_id = scheme.get_id() if scheme is not None else ""
        self._source_scheme_id = scheme_id
        for source_buffer in list(self._source_buffers.values()):
            current = source_buffer.get_style_scheme()
            current_id = current.get_id() if current is not None else ""
            if current_id != scheme_id:
                source_buffer.set_style_scheme(scheme)

    def connect_selection_changed(
        self, callback: Callable[["MarkdownView", str], object]
    ) -> int:
        """Call *callback(view, selected_text)* whenever selection changes."""
        return self.connect("selection-changed", callback)

    def connect_selection_context_requested(
        self,
        callback: Callable[
            ["MarkdownView", str, Optional[object], Gtk.Widget, float, float],
            object,
        ],
    ) -> int:
        """Call *callback(view, text, event, widget, root_x, root_y)*."""
        return self.connect("selection-context-requested", callback)

    def request_selection_context(
        self,
        event: Optional[object] = None,
        source_widget: Optional[Gtk.Widget] = None,
        root_x: Optional[float] = None,
        root_y: Optional[float] = None,
    ) -> bool:
        """Request the message-level context menu for the current selection.

        ``root_x`` and ``root_y`` are screen coordinates.  ``event`` is the
        original ``Gdk.EventButton`` for pointer invocation (or ``None`` for
        the keyboard menu key), and ``source_widget`` is always included in
        the emitted signal.  Callers may omit coordinates for keyboard use;
        the signal is then anchored near the first selected block.  Nothing
        is emitted when this view does not own a non-empty selection.
        """
        selected = self.get_selected_text()
        if not selected:
            return False
        if root_x is None or root_y is None:
            selected_widget = self._active_selection_widget
            if selected_widget not in self._selection_widgets:
                selected_widget = next(
                    (
                        widget
                        for widget in self._selection_widgets
                        if self._widget_selection(widget) is not None
                    ),
                    self._selection_widgets[0] if self._selection_widgets else None,
                )
            if source_widget is None:
                source_widget = selected_widget
            rect = (
                self._widget_root_rect(selected_widget)
                if selected_widget is not None
                else None
            )
            if rect is None:
                root_x, root_y = 0.0, 0.0
            else:
                root_x = rect[0] + min(24.0, rect[2] / 2.0)
                root_y = rect[1] + min(24.0, rect[3] / 2.0)
        if source_widget is None:
            source_widget = self
        self.emit(
            "selection-context-requested",
            selected,
            event,
            source_widget,
            float(root_x),
            float(root_y),
        )
        return True

    def get_selected_text(self) -> str:
        """Return GTK's current native selection for this message."""
        if not self._owns_native_selection():
            return ""
        active = self._active_selection_widget
        if active in self._selection_widgets:
            selected = self._widget_selection(active)
            if selected is not None:
                return selected.text
            cached = self._cached_native_selection(active)
            if cached is not None:
                # CODEX: GtkLabel can drop its PRIMARY range after selection notifications.
                # CODEX: Recover it natively while retaining the gesture text for callers.
                self._recover_cached_native_selection(active)
                return cached.text
        for widget in self._selection_widgets:
            selected = self._widget_selection(widget)
            if selected is not None:
                self._active_selection_widget = widget
                return selected.text
        return ""

    def clear_selection(self) -> None:
        """Clear the native range without manufacturing a replacement range."""
        self._clear_native_selection_cache()
        self._clearing_selection = True
        try:
            for widget in list(self._selection_widgets):
                if isinstance(widget, Gtk.Label):
                    widget.select_region(0, 0)
                elif isinstance(widget, Gtk.TextView):
                    text_buffer = widget.get_buffer()
                    start = text_buffer.get_start_iter()
                    text_buffer.select_range(start, start)
            self._active_selection_widget = None
        finally:
            self._clearing_selection = False
        self._release_native_selection_owner()
        self._queue_selection_changed()

    def select_all_prose(self) -> None:
        """Select the active readable block with GTK's native range."""
        self._claim_native_selection_owner()
        self._clear_native_selection_cache()
        widget = self._active_selection_widget
        if widget not in self._selection_widgets:
            widget = next(
                (item for item in self._selection_widgets if isinstance(item, Gtk.Label)),
                None,
            )
        if isinstance(widget, Gtk.Label):
            widget.grab_focus()
            widget.select_region(0, -1)
            self._active_selection_widget = widget
        elif isinstance(widget, Gtk.TextView):
            buffer = widget.get_buffer()
            widget.grab_focus()
            buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
            self._active_selection_widget = widget
        self._queue_selection_changed()

    def _widget_selection(
        self, widget: Gtk.Widget
    ) -> Optional[_SelectionSnapshot]:
        if isinstance(widget, Gtk.Label):
            selected, start, end = widget.get_selection_bounds()
            if not selected:
                return None
            # Pango label positions are character offsets.  Strip only the
            # invisible bidi controls inserted by this renderer.
            text = BIDI_CONTROL_RE.sub("", widget.get_text()[start:end])
            return _SelectionSnapshot(start, end, text)
        if isinstance(widget, Gtk.TextView):
            text_buffer = widget.get_buffer()
            bounds = text_buffer.get_selection_bounds()
            if not bounds:
                return None
            start, end = bounds
            return _SelectionSnapshot(
                start.get_offset(),
                end.get_offset(),
                text_buffer.get_text(start, end, True),
            )
        return None

    def _set_remembered_highlight(
        self, widget: Gtk.Widget, snapshot: Optional[_SelectionSnapshot]
    ) -> None:
        background_color, foreground_color = self._selection_colors(widget)
        if isinstance(widget, Gtk.Label):
            if snapshot is None:
                widget.set_attributes(None)
                return
            label_text = widget.get_text()
            start_byte = len(label_text[: snapshot.start].encode("utf-8"))
            end_byte = len(label_text[: snapshot.end].encode("utf-8"))
            attributes = Pango.AttrList()
            background = Pango.attr_background_new(*background_color)
            foreground = Pango.attr_foreground_new(*foreground_color)
            for attribute in (background, foreground):
                attribute.start_index = start_byte
                attribute.end_index = end_byte
                attributes.insert(attribute)
            widget.set_attributes(attributes)
            return
        if isinstance(widget, Gtk.TextView):
            text_buffer = widget.get_buffer()
            stored = self._selection_tags.get(widget)
            tag = (
                stored[1]
                if stored is not None and stored[0] is text_buffer
                else None
            )
            background_hex = self._pango_color_hex(background_color)
            foreground_hex = self._pango_color_hex(foreground_color)
            if tag is None:
                tag = text_buffer.create_tag(
                    None,
                    background=background_hex,
                    foreground=foreground_hex,
                )
                self._selection_tags[widget] = (text_buffer, tag)
            else:
                # A message can stay alive while the app switches themes.
                tag.set_property("background", background_hex)
                tag.set_property("foreground", foreground_hex)
            text_buffer.remove_tag(
                tag, text_buffer.get_start_iter(), text_buffer.get_end_iter()
            )
            if snapshot is not None:
                text_buffer.apply_tag(
                    tag,
                    text_buffer.get_iter_at_offset(snapshot.start),
                    text_buffer.get_iter_at_offset(snapshot.end),
                )

    @staticmethod
    def _pango_color_hex(color: Tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*(channel // 257 for channel in color))

    @staticmethod
    def _selection_colors(
        widget: Gtk.Widget,
    ) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        """Use the active GTK theme's semantic selection colors when present."""
        context = widget.get_style_context()

        def lookup(name: str, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
            found, rgba = context.lookup_color(name)
            if not found:
                return fallback
            return (
                max(0, min(0xFFFF, round(rgba.red * 0xFFFF))),
                max(0, min(0xFFFF, round(rgba.green * 0xFFFF))),
                max(0, min(0xFFFF, round(rgba.blue * 0xFFFF))),
            )

        return (
            lookup("theme_selected_bg_color", SELECTION_BLUE),
            lookup("theme_selected_fg_color", SELECTION_WHITE),
        )

    def _clear_remembered_selections(self) -> None:
        for widget in list(self._remembered_selections):
            self._set_remembered_highlight(widget, None)
        self._remembered_selections.clear()

    def _clear_native_selection_fallbacks(self) -> None:
        for widget in list(self._native_selection_fallbacks):
            if widget not in self._remembered_selections:
                self._set_remembered_highlight(widget, None)
        self._native_selection_fallbacks.clear()

    def _cancel_native_selection_recovery(self) -> None:
        source = self._native_selection_recovery_source
        self._native_selection_recovery_source = None
        if source is not None:
            try:
                GLib.source_remove(source)
            except GLib.Error:
                pass

    def _cancel_native_selection_verify(self) -> None:
        source = self._native_selection_verify_source
        self._native_selection_verify_source = None
        if source is not None:
            try:
                GLib.source_remove(source)
            except GLib.Error:
                pass

    def _clear_native_selection_cache(self) -> None:
        self._native_selection_cache = None
        self._native_selection_cache_armed = False
        self._cancel_native_selection_recovery()
        self._cancel_native_selection_verify()

    @classmethod
    def _current_native_selection_owner(cls) -> Optional["MarkdownView"]:
        owner_ref = cls._native_selection_owner
        owner = owner_ref() if owner_ref is not None else None
        if owner is None:
            cls._native_selection_owner = None
        return owner

    def _owns_native_selection(self) -> bool:
        owner = type(self)._current_native_selection_owner()
        return owner is None or owner is self

    def _relinquish_native_selection_owner(self) -> None:
        self._clear_native_selection_cache()
        self._active_selection_widget = None
        self._queue_selection_changed()

    def _claim_native_selection_owner(self) -> None:
        owner = type(self)._current_native_selection_owner()
        if owner is self:
            return
        if owner is not None:
            owner._relinquish_native_selection_owner()
        type(self)._native_selection_owner = weakref.ref(self)

    def _release_native_selection_owner(self) -> None:
        if type(self)._current_native_selection_owner() is self:
            type(self)._native_selection_owner = None

    def _begin_native_primary_gesture(self) -> None:
        self._claim_native_selection_owner()
        self._clear_native_selection_cache()
        self._native_selection_cache_armed = True

    def _cache_native_selection(
        self, widget: Gtk.Widget, selection: _SelectionSnapshot
    ) -> None:
        if self._native_selection_cache_armed:
            self._cancel_native_selection_recovery()
            self._native_selection_cache = (weakref.ref(widget), selection)
            self._recover_cached_native_selection(widget)
            self._schedule_native_selection_verify(widget)

    def _cached_native_selection(
        self, widget: Gtk.Widget
    ) -> Optional[_SelectionSnapshot]:
        cached = self._native_selection_cache
        if (
            cached is None
            or not self._native_selection_cache_armed
            or cached[0]() is not widget
            or widget not in self._selection_widgets
            or not widget.get_mapped()
        ):
            return None
        snapshot = cached[1]
        if (
            snapshot.start >= snapshot.end
            or snapshot.end > len(self._widget_text(widget))
        ):
            self._clear_native_selection_cache()
            return None
        return snapshot

    def _recover_cached_native_selection(self, widget: Gtk.Widget) -> None:
        snapshot = self._cached_native_selection(widget)
        cached = self._native_selection_cache
        if (
            snapshot is None
            or cached is None
            or self._native_selection_recovering
        ):
            return
        if self._native_selection_recovery_source is not None:
            return

        def restore() -> bool:
            self._native_selection_recovery_source = None
            if self._native_selection_cache is not cached:
                return GLib.SOURCE_REMOVE
            target = cached[0]()
            if (
                target is not widget
                or target not in self._selection_widgets
                or not target.get_mapped()
                or self._widget_selection(target) is not None
            ):
                return GLib.SOURCE_REMOVE
            self._native_selection_recovering = True
            try:
                if isinstance(target, Gtk.Label):
                    target.select_region(snapshot.start, snapshot.end)
                elif isinstance(target, Gtk.TextView):
                    buffer = target.get_buffer()
                    buffer.select_range(
                        buffer.get_iter_at_offset(snapshot.start),
                        buffer.get_iter_at_offset(snapshot.end),
                    )
                self._active_selection_widget = target
            finally:
                self._native_selection_recovering = False
            self._queue_selection_changed()
            return GLib.SOURCE_REMOVE

        self._native_selection_recovery_source = GLib.idle_add(
            restore, priority=GLib.PRIORITY_LOW
        )

    def _schedule_native_selection_verify(self, widget: Gtk.Widget) -> None:
        if (
            self._cached_native_selection(widget) is None
            or self._native_selection_verify_source is not None
        ):
            return

        def verify() -> bool:
            self._native_selection_verify_source = None
            snapshot = self._cached_native_selection(widget)
            if snapshot is None or not self._owns_native_selection():
                return GLib.SOURCE_REMOVE
            if self._widget_selection(widget) is not None:
                return GLib.SOURCE_REMOVE
            self._native_selection_recovering = True
            try:
                if isinstance(widget, Gtk.Label):
                    widget.select_region(snapshot.start, snapshot.end)
                elif isinstance(widget, Gtk.TextView):
                    buffer = widget.get_buffer()
                    buffer.select_range(
                        buffer.get_iter_at_offset(snapshot.start),
                        buffer.get_iter_at_offset(snapshot.end),
                    )
                self._active_selection_widget = widget
            finally:
                self._native_selection_recovering = False
            self._queue_selection_changed()
            return GLib.SOURCE_REMOVE

        self._native_selection_verify_source = GLib.timeout_add(
            1, verify, priority=GLib.PRIORITY_LOW
        )

    def _on_native_selection_changed(
        self, widget: Gtk.Widget, *_args: object
    ) -> None:
        """Expose GTK's real selection without synthetic fallback highlights."""
        if self._clearing_selection:
            return
        selected = self._widget_selection(widget)
        if selected is not None:
            self._claim_native_selection_owner()
            self._active_selection_widget = widget
            self._cache_native_selection(widget, selected)
        elif self._native_selection_cache is not None:
            self._recover_cached_native_selection(widget)
            return
        elif self._active_selection_widget is widget:
            self._active_selection_widget = None
        self._queue_selection_changed()

    def _queue_selection_changed(self, *_args: object) -> None:
        if self._clearing_selection or self._selection_emit_source is not None:
            return

        def emit_selection() -> bool:
            self._selection_emit_source = None
            self.emit("selection-changed", self.get_selected_text())
            return GLib.SOURCE_REMOVE

        self._selection_emit_source = GLib.idle_add(emit_selection)

    def _cancel_plain_release_finalize(self) -> None:
        source = self._plain_release_source
        self._plain_release_source = None
        if source is not None:
            try:
                GLib.source_remove(source)
            except GLib.Error:
                pass

    def _schedule_plain_release_finalize(self) -> None:
        self._cancel_plain_release_finalize()

        def finalize() -> bool:
            self._plain_release_source = None
            self._plain_pointer_active = False
            if self._document_selection_active:
                self._queue_selection_changed()
                return GLib.SOURCE_REMOVE
            native = [
                (widget, self._widget_selection(widget))
                for widget in self._selection_widgets
            ]
            native = [(widget, snapshot) for widget, snapshot in native if snapshot]
            if native:
                self._claim_selection_ownership()
                for widget, snapshot in native:
                    assert snapshot is not None
                    self._native_selection_fallbacks[widget] = snapshot
                self._queue_selection_changed()
            else:
                # The default GtkLabel/GtkTextView handler treated this as a
                # caret click, so discard every logical remnant as well.
                self.clear_selection()
            return GLib.SOURCE_REMOVE

        self._plain_release_source = GLib.idle_add(finalize)

    def _remember_current_selection(self) -> None:
        for widget in self._selection_widgets:
            selected = self._widget_selection(widget)
            if selected:
                self._remembered_selections[widget] = selected
                self._set_remembered_highlight(widget, selected)

    @staticmethod
    def _widget_text(widget: Gtk.Widget) -> str:
        if isinstance(widget, Gtk.Label):
            return widget.get_text()
        if isinstance(widget, Gtk.TextView):
            text_buffer = widget.get_buffer()
            return text_buffer.get_text(
                text_buffer.get_start_iter(), text_buffer.get_end_iter(), True
            )
        return ""

    def _selection_snapshot(
        self, widget: Gtk.Widget, start: int, end: int
    ) -> Optional[_SelectionSnapshot]:
        rendered = self._widget_text(widget)
        start = max(0, min(len(rendered), int(start)))
        end = max(0, min(len(rendered), int(end)))
        if end < start:
            start, end = end, start
        if start == end:
            return None
        return _SelectionSnapshot(
            start,
            end,
            BIDI_CONTROL_RE.sub("", rendered[start:end]),
        )

    def _clear_native_selections(self) -> None:
        for widget in self._selection_widgets:
            if isinstance(widget, Gtk.Label):
                widget.select_region(0, 0)
            elif isinstance(widget, Gtk.TextView):
                text_buffer = widget.get_buffer()
                start = text_buffer.get_start_iter()
                text_buffer.select_range(start, start)

    def _select_document_range(
        self,
        anchor_widget: Gtk.Widget,
        anchor_offset: int,
        target_widget: Gtk.Widget,
        target_offset: int,
        preserve_existing: bool = False,
    ) -> bool:
        """Select one continuous document range across rendered child widgets.

        GTK3 cannot natively extend a selection from one ``Gtk.Label`` or
        ``Gtk.TextView`` into its sibling.  Store equivalent ranges on every
        widget between the pointer anchor and endpoint, in document order.
        """
        try:
            anchor_index = self._selection_widgets.index(anchor_widget)
            target_index = self._selection_widgets.index(target_widget)
        except ValueError:
            return False

        anchor_offset = max(
            0, min(len(self._widget_text(anchor_widget)), int(anchor_offset))
        )
        target_offset = max(
            0, min(len(self._widget_text(target_widget)), int(target_offset))
        )
        if (anchor_index, anchor_offset) <= (target_index, target_offset):
            first_index, first_offset = anchor_index, anchor_offset
            last_index, last_offset = target_index, target_offset
        else:
            first_index, first_offset = target_index, target_offset
            last_index, last_offset = anchor_index, anchor_offset

        self._clearing_selection = True
        try:
            self._claim_selection_ownership()
            self._clear_native_selection_fallbacks()
            if not preserve_existing:
                self._clear_remembered_selections()
            self._clear_native_selections()
            for index in range(first_index, last_index + 1):
                widget = self._selection_widgets[index]
                start = first_offset if index == first_index else 0
                end = last_offset if index == last_index else len(
                    self._widget_text(widget)
                )
                snapshot = self._selection_snapshot(widget, start, end)
                if snapshot is None:
                    continue
                self._remembered_selections[widget] = snapshot
                self._set_remembered_highlight(widget, snapshot)
            self._document_selection_active = bool(self._remembered_selections)
        finally:
            self._clearing_selection = False
        self._queue_selection_changed()
        return True

    @staticmethod
    def _window_origin(window: Gdk.Window) -> Tuple[int, int]:
        origin = window.get_origin()
        # PyGObject has exposed both ``(x, y)`` and ``(success, x, y)`` for
        # this API across supported GTK releases.
        if len(origin) == 3:
            return int(origin[1]), int(origin[2])
        return int(origin[0]), int(origin[1])

    def _widget_root_rect(
        self, widget: Gtk.Widget
    ) -> Optional[Tuple[float, float, float, float]]:
        if not widget.get_realized():
            return None
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Widget) or toplevel.get_window() is None:
            return None
        translated = widget.translate_coordinates(toplevel, 0, 0)
        if translated is None:
            return None
        origin_x, origin_y = self._window_origin(toplevel.get_window())
        allocation = widget.get_allocation()
        return (
            float(origin_x + translated[0]),
            float(origin_y + translated[1]),
            float(max(1, allocation.width)),
            float(max(1, allocation.height)),
        )

    def _widget_at_root(self, root_x: float, root_y: float) -> Optional[Gtk.Widget]:
        candidates: List[Tuple[Gtk.Widget, Tuple[float, float, float, float]]] = []
        for widget in self._selection_widgets:
            if not widget.get_mapped():
                continue
            rect = self._widget_root_rect(widget)
            if rect is None:
                continue
            left, top, width, height = rect
            if left <= root_x <= left + width and top <= root_y <= top + height:
                return widget
            candidates.append((widget, rect))
        if not candidates:
            return None

        # Text selection remains useful when the pointer moves through the
        # spacing between blocks or beyond the side of a narrow line.  Pick
        # the nearest block instead of dropping the in-progress gesture.
        def distance(
            item: Tuple[Gtk.Widget, Tuple[float, float, float, float]]
        ) -> Tuple[float, float]:
            _widget, (left, top, width, height) = item
            vertical = max(top - root_y, 0.0, root_y - (top + height))
            horizontal = max(left - root_x, 0.0, root_x - (left + width))
            return vertical, horizontal

        return min(candidates, key=distance)[0]

    @staticmethod
    def _label_layout_origin(label: Gtk.Label) -> Tuple[int, int]:
        offset_x, offset_y = label.get_layout_offsets()
        if not label.get_has_window():
            # Gtk.Label is normally windowless, in which case GTK reports the
            # layout offset in its shared parent-window coordinates.
            allocation = label.get_allocation()
            offset_x -= allocation.x
            offset_y -= allocation.y
        return int(offset_x), int(offset_y)

    @classmethod
    def _label_offset_from_local(cls, label: Gtk.Label, x: float, y: float) -> int:
        rendered = label.get_text()
        layout = label.get_layout()
        offset_x, offset_y = cls._label_layout_origin(label)
        natural_width, layout_height = layout.get_pixel_size()
        configured_width = layout.get_width()
        # CODEX: A right-aligned Pango layout reports glyph x positions in its
        # CODEX: full configured width, not its natural ink width.
        layout_width = (
            configured_width / Pango.SCALE
            if configured_width > 0
            else natural_width
        )
        if y < offset_y:
            return 0
        if y > offset_y + layout_height:
            return len(rendered)
        local_x = max(0.0, min(float(max(0, layout_width - 1)), x - offset_x))
        local_y = max(0.0, min(float(max(0, layout_height - 1)), y - offset_y))
        _inside, byte_index, trailing = layout.xy_to_index(
            int(local_x * Pango.SCALE), int(local_y * Pango.SCALE)
        )
        # Pango indexes UTF-8 bytes; Gtk.Label selection uses characters.
        character_index = len(
            rendered.encode("utf-8")[:byte_index].decode("utf-8", errors="ignore")
        )
        return max(0, min(len(rendered), character_index + int(trailing)))

    def _widget_offset_from_local(
        self, widget: Gtk.Widget, x: float, y: float
    ) -> int:
        if isinstance(widget, Gtk.Label):
            return self._label_offset_from_local(widget, x, y)
        if isinstance(widget, Gtk.TextView):
            rendered = self._widget_text(widget)
            allocation = widget.get_allocation()
            if y < 0:
                return 0
            if y > allocation.height:
                return len(rendered)
            buffer_x, buffer_y = widget.window_to_buffer_coords(
                Gtk.TextWindowType.WIDGET, int(max(0.0, x)), int(max(0.0, y))
            )
            result = widget.get_iter_at_location(buffer_x, buffer_y)
            text_iter = result[-1]
            return max(0, min(len(rendered), text_iter.get_offset()))
        return 0

    def _widget_offset_at_root(
        self, widget: Gtk.Widget, root_x: float, root_y: float
    ) -> int:
        rect = self._widget_root_rect(widget)
        if rect is None:
            return 0
        left, top, _width, _height = rect
        return self._widget_offset_from_local(widget, root_x - left, root_y - top)

    def _event_root_position(
        self, widget: Gtk.Widget, event: object
    ) -> Tuple[float, float]:
        root_x = getattr(event, "x_root", None)
        root_y = getattr(event, "y_root", None)
        if root_x is not None and root_y is not None:
            return float(root_x), float(root_y)
        rect = self._widget_root_rect(widget)
        if rect is None:
            return float(getattr(event, "x", 0.0)), float(
                getattr(event, "y", 0.0)
            )
        return (
            rect[0] + float(getattr(event, "x", 0.0)),
            rect[1] + float(getattr(event, "y", 0.0)),
        )

    def _outer_vertical_scroller(self) -> Optional[Gtk.ScrolledWindow]:
        """Find the transcript scroller, skipping nested code/table scrollers."""
        parent = self.get_parent()
        candidate: Optional[Gtk.ScrolledWindow] = None
        while parent is not None:
            if isinstance(parent, Gtk.ScrolledWindow):
                adjustment = parent.get_vadjustment()
                if adjustment is not None:
                    candidate = parent
            parent = parent.get_parent()
        return candidate

    @staticmethod
    def _edge_scroll_delta(
        pointer_y: float, top: float, height: float, edge: float = 54.0
    ) -> float:
        """Return a smooth signed scroll step for a pointer near a viewport edge."""
        safe_edge = max(12.0, min(edge, max(12.0, height / 3.0)))
        upper_edge = top + safe_edge
        lower_edge = top + height - safe_edge
        if pointer_y < upper_edge:
            pressure = min(1.6, max(0.0, (upper_edge - pointer_y) / safe_edge))
            return -(5.0 + 27.0 * pressure)
        if pointer_y > lower_edge:
            pressure = min(1.6, max(0.0, (pointer_y - lower_edge) / safe_edge))
            return 5.0 + 27.0 * pressure
        return 0.0

    def _stop_drag_autoscroll(self) -> None:
        source = self._drag_scroll_source
        self._drag_scroll_source = None
        self._drag_pointer_root = None
        if source is not None:
            try:
                GLib.source_remove(source)
            except GLib.Error:
                pass

    def _update_drag_selection_at_pointer(self) -> bool:
        if self._drag_anchor is None or self._drag_pointer_root is None:
            return False
        root_x, root_y = self._drag_pointer_root
        target = self._widget_at_root(root_x, root_y)
        if target is None:
            return False
        anchor_widget, anchor_offset = self._drag_anchor
        target_offset = self._widget_offset_at_root(target, root_x, root_y)
        if (
            isinstance(anchor_widget, Gtk.Label)
            and self._manual_prose_gesture
            and target is anchor_widget
            and target_offset == anchor_offset
        ):
            return False
        if isinstance(anchor_widget, Gtk.Label) and self._manual_prose_gesture:
            self._restore_manual_prose_preserved()
        self._drag_aggregate = True
        return self._select_document_range(
            anchor_widget,
            anchor_offset,
            target,
            target_offset,
            preserve_existing=(
                isinstance(anchor_widget, Gtk.Label)
                and self._manual_prose_gesture
                and self._manual_prose_preserve_existing
            ),
        )

    def _drag_autoscroll_tick(self) -> bool:
        if self._drag_anchor is None or self._drag_pointer_root is None:
            self._drag_scroll_source = None
            return GLib.SOURCE_REMOVE
        scroller = self._outer_vertical_scroller()
        if scroller is None or not scroller.get_mapped():
            self._drag_scroll_source = None
            return GLib.SOURCE_REMOVE
        rect = self._widget_root_rect(scroller)
        if rect is None:
            return GLib.SOURCE_CONTINUE
        _left, top, _width, height = rect
        delta = self._edge_scroll_delta(self._drag_pointer_root[1], top, height)
        if not delta:
            self._drag_scroll_source = None
            return GLib.SOURCE_REMOVE
        adjustment = scroller.get_vadjustment()
        lower = adjustment.get_lower()
        upper = max(lower, adjustment.get_upper() - adjustment.get_page_size())
        before = adjustment.get_value()
        after = max(lower, min(upper, before + delta))
        if abs(after - before) > 0.01:
            adjustment.set_value(after)
            # Allocation/root positions move as the viewport scrolls. Re-hit-test
            # the fixed pointer after every step so the selected range grows too.
            self._update_drag_selection_at_pointer()
        return GLib.SOURCE_CONTINUE

    def _arm_drag_autoscroll(self, root_x: float, root_y: float) -> None:
        self._drag_pointer_root = (root_x, root_y)
        scroller = self._outer_vertical_scroller()
        rect = self._widget_root_rect(scroller) if scroller is not None else None
        delta = (
            self._edge_scroll_delta(root_y, rect[1], rect[3])
            if rect is not None
            else 0.0
        )
        if delta and self._drag_scroll_source is None:
            self._drag_scroll_source = GLib.timeout_add(32, self._drag_autoscroll_tick)
        elif not delta and self._drag_scroll_source is not None:
            source = self._drag_scroll_source
            self._drag_scroll_source = None
            GLib.source_remove(source)

    @staticmethod
    def _is_prose_word_character(character: str) -> bool:
        """Return whether a character belongs to a natural-language word."""
        return bool(
            character
            and (
                character.isalnum()
                or character in {"_", "\u200c", "\u200d"}
                or unicodedata.category(character).startswith("M")
            )
        )

    @classmethod
    def _prose_word_bounds(cls, label: Gtk.Label, offset: int) -> Tuple[int, int]:
        """Find the clicked Unicode word without depending on GTK focus state."""
        rendered = label.get_text()
        offset = max(0, min(len(rendered), int(offset)))
        candidate = offset
        if candidate >= len(rendered) or not cls._is_prose_word_character(
            rendered[candidate]
        ):
            candidate -= 1
        if candidate < 0 or not cls._is_prose_word_character(rendered[candidate]):
            return offset, offset
        start = candidate
        end = candidate + 1
        while start > 0 and cls._is_prose_word_character(rendered[start - 1]):
            start -= 1
        while end < len(rendered) and cls._is_prose_word_character(rendered[end]):
            end += 1
        return start, end

    @staticmethod
    def _utf8_character_offset(rendered: str, byte_offset: int) -> int:
        """Translate Pango's UTF-8 byte positions to GtkLabel character offsets."""
        byte_offset = max(0, min(len(rendered.encode("utf-8")), int(byte_offset)))
        return len(
            rendered.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore")
        )

    @classmethod
    def _prose_visual_line_bounds(
        cls, label: Gtk.Label, offset: int, local_y: Optional[float] = None
    ) -> Tuple[int, int]:
        """Return a physical Pango line, falling back to the source line."""
        rendered = label.get_text()
        if not rendered:
            return 0, 0
        byte_offset = len(rendered[: max(0, min(len(rendered), offset))].encode("utf-8"))
        layout = label.get_layout()
        lines = list(layout.get_lines_readonly())
        chosen: Optional[Pango.LayoutLine] = None
        if local_y is not None:
            _layout_x, layout_y = cls._label_layout_origin(label)
            content_y = float(local_y) - float(layout_y)
            for line in lines:
                position = layout.index_to_pos(line.get_start_index())
                line_y = position.y / Pango.SCALE
                line_height = max(1.0, position.height / Pango.SCALE)
                if line_y <= content_y < line_y + line_height:
                    chosen = line
                    break
        if chosen is None:
            for line in lines:
                start = line.get_start_index()
                end = start + line.get_length()
                if start <= byte_offset < end or (
                    byte_offset == end and end == len(rendered.encode("utf-8"))
                ):
                    chosen = line
                    break
        if chosen is not None:
            start = cls._utf8_character_offset(rendered, chosen.get_start_index())
            end = cls._utf8_character_offset(
                rendered, chosen.get_start_index() + chosen.get_length()
            )
            if start != end:
                return start, end
        start = rendered.rfind("\n", 0, offset) + 1
        end = rendered.find("\n", offset)
        return start, len(rendered) if end < 0 else end

    def _prepare_manual_prose_selection(self, preserve_existing: bool) -> None:
        """Clear GTK's native label range before a caret-free pointer gesture."""
        self._clearing_selection = True
        try:
            if preserve_existing:
                self._remember_current_selection()
                self._manual_prose_preserved = dict(self._remembered_selections)
            else:
                self._manual_prose_preserved.clear()
            self._clear_native_selection_fallbacks()
            if not preserve_existing:
                self._clear_remembered_selections()
            self._clear_native_selections()
            self._document_selection_active = bool(self._remembered_selections)
            if not preserve_existing:
                self._release_selection_ownership()
        finally:
            self._clearing_selection = False

    def _restore_manual_prose_preserved(self) -> None:
        """Keep Control-added ranges stable while a new prose drag moves."""
        if not self._manual_prose_preserve_existing:
            return
        for widget in list(self._remembered_selections):
            if widget not in self._manual_prose_preserved:
                self._set_remembered_highlight(widget, None)
                self._remembered_selections.pop(widget, None)
        for widget, snapshot in self._manual_prose_preserved.items():
            if widget in self._selection_widgets:
                self._remembered_selections[widget] = snapshot
                self._set_remembered_highlight(widget, snapshot)

    def _reset_manual_prose_gesture(self) -> None:
        self._manual_prose_gesture = False
        self._manual_prose_preserve_existing = False
        self._manual_prose_preserved.clear()

    def _select_prose_multiclick(
        self, label: Gtk.Label, event: Gdk.EventButton, line: bool
    ) -> bool:
        root_x, root_y = self._event_root_position(label, event)
        offset = self._widget_offset_at_root(label, root_x, root_y)
        if line:
            rect = self._widget_root_rect(label)
            local_y = root_y - rect[1] if rect is not None else None
            start, end = self._prose_visual_line_bounds(label, offset, local_y)
        else:
            start, end = self._prose_word_bounds(label, offset)
        label.grab_focus()
        label.select_region(start, end)
        self._active_selection_widget = label if start != end else None
        self._queue_selection_changed()
        return True

    def _on_prose_button_press(
        self, label: Gtk.Label, event: Gdk.EventButton
    ) -> bool:
        if not label.get_can_focus():
            label.set_can_focus(True)
        event_type = event.type
        if event_type == Gdk.EventType._3BUTTON_PRESS:
            return self._select_prose_multiclick(label, event, line=True)
        if event_type == Gdk.EventType._2BUTTON_PRESS:
            return self._select_prose_multiclick(label, event, line=False)
        return False

    def _on_selectable_button_press(
        self, _widget: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        if event.button == Gdk.BUTTON_SECONDARY:
            root_x, root_y = self._event_root_position(_widget, event)
            # Consuming the event is important: GtkLabel/GtkTextView's own
            # popup handler can otherwise collapse the range before the
            # message-level Reply/Ask menu reads it.
            return self.request_selection_context(
                event=event,
                source_widget=_widget,
                root_x=root_x,
                root_y=root_y,
            )
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        self._begin_native_primary_gesture()
        if isinstance(_widget, Gtk.Label):
            return self._on_prose_button_press(_widget, event)
        return False

    def _on_selectable_motion(
        self, widget: Gtk.Widget, event: Gdk.EventMotion
    ) -> bool:
        return False

    def _on_selectable_button_release(
        self, widget: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        return False

    def _on_selectable_key_press(
        self, widget: Gtk.Widget, event: Gdk.EventKey
    ) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if self.get_selected_text() or self._native_selection_cache is not None:
                self.clear_selection()
                return True
        if event.keyval in (Gdk.KEY_Menu,) or (
            event.keyval in (Gdk.KEY_F10,)
            and bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ):
            return self.request_selection_context(source_widget=widget)
        return False

    def _register_selection_widget(self, widget: Gtk.Widget) -> None:
        self._selection_widgets.append(widget)
        widget.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        widget.connect("button-press-event", self._on_selectable_button_press)
        widget.connect("key-press-event", self._on_selectable_key_press)
        widget.connect(
            "popup-menu",
            lambda source: self.request_selection_context(source_widget=source),
        )
        if isinstance(widget, Gtk.Label):
            widget.connect(
                "notify::cursor-position", self._on_native_selection_changed
            )
            widget.connect(
                "notify::selection-bound", self._on_native_selection_changed
            )
        elif isinstance(widget, Gtk.TextView):
            widget.get_buffer().connect(
                "mark-set",
                lambda _buffer, _location, _mark, target=widget: (
                    self._on_native_selection_changed(target)
                ),
            )

    def _unregister_selection_subtree(self, root: Gtk.Widget) -> None:
        removed = [
            widget
            for widget in self._selection_widgets
            if widget is root or widget.is_ancestor(root)
        ]
        if not removed:
            return
        for widget in removed:
            if isinstance(widget, Gtk.TextView):
                self._selection_tags.pop(widget, None)
                self._source_buffers.pop(widget, None)
        removed_set = set(removed)
        self._selection_widgets = [
            widget for widget in self._selection_widgets if widget not in removed_set
        ]
        cached = self._native_selection_cache
        if cached is not None and cached[0]() in removed_set:
            self._clear_native_selection_cache()
        if self._active_selection_widget in removed_set:
            self._active_selection_widget = None
            self._release_native_selection_owner()
        self._queue_selection_changed()

    def set_text(self, text: str) -> None:
        display_text = (text or "…").replace("\r\n", "\n").replace("\r", "\n")
        blocks = split_blocks(display_text)
        new_direction = _message_is_rtl(blocks, prefer_rtl=self.user_message)
        stable_cut = _stable_prefix_length(display_text)

        can_extend = (
            self._rendered_text is not None
            and new_direction == self.message_rtl
            and display_text.startswith(self._stable_text)
            and stable_cut >= len(self._stable_text)
        )
        if can_extend:
            for child in self._tail_widgets:
                if child.get_parent() is self:
                    self._unregister_selection_subtree(child)
                    self.remove(child)
            self._tail_widgets.clear()
            stable_delta = display_text[len(self._stable_text) : stable_cut]
            if stable_delta:
                self._render_fragment(stable_delta)
            tail_start = len(self.get_children())
            tail = display_text[stable_cut:]
            if tail:
                self._render_fragment(tail)
            self._tail_widgets = self.get_children()[tail_start:]
            self._stable_text = display_text[:stable_cut]
            self._rendered_text = display_text
            self.show_all()
            return

        self.message_rtl = new_direction
        # Preserve physical/document order at the container level.  Individual
        # prose blocks choose their own direction, so one RTL paragraph cannot
        # mirror unrelated English headings, lists, or tables in the response.
        self.set_direction(Gtk.TextDirection.LTR)
        for child in self.get_children():
            self._unregister_selection_subtree(child)
            self.remove(child)
        if stable_cut:
            self._render_fragment(display_text[:stable_cut])
        tail_start = len(self.get_children())
        if display_text[stable_cut:]:
            self._render_fragment(display_text[stable_cut:])
        self._tail_widgets = self.get_children()[tail_start:]
        self._stable_text = display_text[:stable_cut]
        self._rendered_text = display_text
        self.show_all()

    def _render_fragment(self, text: str) -> None:
        for kind, language, content in split_blocks(text):
            if kind == "code":
                self.pack_start(self._code_block(language, content), False, False, 0)
            elif kind == "table":
                self.pack_start(self._table_block(content), False, False, 0)
            else:
                self._add_prose(content)

    def _pack_prose_label(self, raw: str, style_class: str = "") -> Gtk.Label:
        rtl = content_prefers_rtl(raw)
        label = self._label(
            inline_markup(raw, rtl_context=rtl), raw, rtl_context=rtl
        )
        if style_class:
            label.get_style_context().add_class(style_class)
        self.pack_start(label, False, False, 0)
        return label

    def _add_prose(self, content: str) -> None:
        lines = content.splitlines() or [""]
        current: List[str] = []

        def flush() -> None:
            if current:
                self._pack_prose_label("\n".join(current))
                current.clear()

        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if (
                stripped
                and index + 1 < len(lines)
                and re.fullmatch(r"\s*(=+|-+)\s*", lines[index + 1])
                and len(lines[index + 1].strip()) >= 3
            ):
                flush()
                size = "x-large" if lines[index + 1].strip().startswith("=") else "large"
                rtl = content_prefers_rtl(stripped)
                markup = inline_markup(stripped, rtl_context=rtl)
                self.pack_start(
                    self._label(
                        f'<span size="{size}" weight="bold">{markup}</span>',
                        stripped,
                        rtl_context=rtl,
                    ),
                    False,
                    False,
                    0,
                )
                index += 2
                continue
            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if heading:
                flush()
                sizes = {
                    1: "xx-large",
                    2: "x-large",
                    3: "large",
                    4: "medium",
                    5: "small",
                    6: "small",
                }
                # Closing hashes are syntax only when separated by whitespace;
                # keep language names such as C# and F# intact.
                raw = re.sub(r"\s+#+\s*$", "", heading.group(2)).rstrip()
                rtl = content_prefers_rtl(raw)
                markup = inline_markup(raw, rtl_context=rtl)
                self.pack_start(
                    self._label(
                        '<span size="{}" weight="bold">{}</span>'.format(
                            sizes[len(heading.group(1))], markup
                        ),
                        raw,
                        rtl_context=rtl,
                    ),
                    False,
                    False,
                    0,
                )
            elif re.fullmatch(r"([-*_])(?:\s*\1){2,}", stripped):
                flush()
                separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                separator.get_style_context().add_class("markdown-rule")
                self.pack_start(separator, False, False, 3)
            else:
                list_item = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.+)$", line)
                if list_item:
                    flush()
                    indent, marker, item = list_item.groups()
                    checklist = re.match(r"^\[([ xX])\]\s*(.*)$", item)
                    if checklist:
                        marker = "☑" if checklist.group(1).lower() == "x" else "☐"
                        item = checklist.group(2)
                    elif not marker[0].isdigit():
                        marker = "•"
                    label = self._pack_prose_label(f"{marker}  {item}", "markdown-list-item")
                    depth = min(4, len(indent.expandtabs(4)) // 2)
                    if label.get_direction() == Gtk.TextDirection.RTL:
                        label.set_margin_end(depth * 18)
                    else:
                        label.set_margin_start(depth * 18)
                elif stripped.startswith(">"):
                    flush()
                    quoted: List[str] = []
                    while index < len(lines) and lines[index].strip().startswith(">"):
                        quoted.append(re.sub(r"^\s*> ?", "", lines[index]))
                        index += 1
                    self._pack_prose_label("\n".join(quoted), "markdown-quote")
                    continue
                elif not stripped:
                    flush()
                else:
                    current.append(line)
            index += 1
        flush()

    def _table_block(self, content: str) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=1, row_spacing=1)
        grid.set_hexpand(True)
        grid.set_column_homogeneous(True)
        # Table columns have a source-defined order.  Cells still choose their
        # own bidi direction, but the grid itself must not be mirrored.
        grid.set_direction(Gtk.TextDirection.LTR)
        grid.get_style_context().add_class("markdown-table")
        rows = [_split_table_row(line) for line in content.splitlines()]
        for row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                cell_markup = inline_markup(cell, rtl_context=is_rtl(cell))
                label = self._label(
                    f"<b>{cell_markup}</b>" if row_index == 0 else cell_markup,
                    cell,
                    max_width_chars=22,
                )
                label.get_style_context().add_class("table-cell")
                label.set_hexpand(True)
                grid.attach(label, column_index, row_index, 1, 1)
        # The table belongs to the message document.  Cells wrap within equal
        # columns so neither axis creates an independent scroll region.
        return grid

    def _activate_link(self, _label: Gtk.Label, uri: str) -> bool:
        if not _is_safe_link(uri):
            return True
        try:
            toplevel = self.get_toplevel()
            window = toplevel if isinstance(toplevel, Gtk.Window) else None
            local_reference = _local_file_reference(uri)
            destination = (
                GLib.filename_to_uri(local_reference[0], None)
                if local_reference is not None
                else uri
            )
            Gtk.show_uri_on_window(window, destination, Gdk.CURRENT_TIME)
        except (GLib.Error, TypeError, ValueError):
            pass
        return True

    @staticmethod
    def _enable_prose_focus_after_map(label: Gtk.Label, *_args: object) -> None:
        def enable() -> bool:
            if label.get_mapped():
                label.set_can_focus(True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(enable)

    def _label(
        self,
        markup: str,
        _raw: str,
        max_width_chars: int = 78,
        rtl_context: Optional[bool] = None,
    ) -> Gtk.Label:
        label = Gtk.Label()
        rtl = is_rtl(_raw) if rtl_context is None else rtl_context
        label.set_direction(Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR)
        label.set_markup(markup)
        label.set_selectable(True)
        label.set_can_focus(False)
        label.set_focus_on_click(True)
        label.set_track_visited_links(False)
        label.get_style_context().add_class("markdown-prose")
        label.connect("map", self._enable_prose_focus_after_map)
        label.connect("activate-link", self._activate_link)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_width_chars(1)
        label.set_max_width_chars(max_width_chars)
        # Gtk mirrors the xalign coordinate for an RTL widget.  Using 1.0 here
        # therefore produces a negative layout offset and draws short RTL
        # lines at the *left* edge.  Anchor the full-width Pango layout at the
        # physical left; Pango.Alignment.RIGHT performs the visible RTL align.
        label.set_xalign(0.0)
        label.set_justify(Gtk.Justification.RIGHT if rtl else Gtk.Justification.LEFT)
        label.set_yalign(0.0)
        label.set_halign(Gtk.Align.FILL)
        # A vertical Gtk.Box otherwise gives short lines only their natural
        # width, which visually pins RTL bullets/paragraphs to the left.
        # Expanding the label makes xalign/justify apply to the whole message.
        label.set_hexpand(True)

        def enforce_layout_direction(widget: Gtk.Label, *_args: object) -> bool:
            # Gtk.Label recreates its Pango.Layout when it is realised and on
            # later allocations.  Reapply these properties at both points;
            # setting them only during construction silently stops working as
            # soon as the message first appears on screen.
            layout = widget.get_layout()
            expected_alignment = (
                Pango.Alignment.RIGHT if rtl else Pango.Alignment.LEFT
            )
            if layout.get_auto_dir():
                layout.set_auto_dir(False)
            if layout.get_alignment() != expected_alignment:
                layout.set_alignment(expected_alignment)
            return False

        label.connect("size-allocate", enforce_layout_direction)
        label.connect("draw", enforce_layout_direction)
        enforce_layout_direction(label)
        self._register_selection_widget(label)
        return label

    def _code_block(self, language: str, code: str) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_hexpand(True)
        outer.set_direction(Gtk.TextDirection.LTR)
        outer.get_style_context().add_class("code-block")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_direction(Gtk.TextDirection.LTR)
        header.get_style_context().add_class("code-header")
        lang = Gtk.Label(label=language or "code")
        lang.set_direction(Gtk.TextDirection.LTR)
        lang.set_xalign(0.0)
        lang.set_selectable(False)
        copy_button = Gtk.Button.new_with_label("Copy")
        copy_button.set_relief(Gtk.ReliefStyle.NONE)
        copy_button.set_tooltip_text("Copy code")
        copy_button.get_style_context().add_class("code-copy")

        def copy_code(button: Gtk.Button) -> None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(code, -1)
            button.set_label("Copied ✓")

            def restore_label() -> bool:
                if button.get_parent() is not None:
                    button.set_label("Copy")
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(1600, restore_label)

        copy_button.connect("clicked", copy_code)
        header.pack_start(lang, True, True, 0)
        header.pack_end(copy_button, False, False, 0)
        outer.pack_start(header, False, False, 0)

        source_buffer: Gtk.TextBuffer
        if GtkSource is not None:
            source_buffer = GtkSource.Buffer()
            source_buffer.set_text(code)
            manager = GtkSource.LanguageManager.get_default()
            aliases = {
                "bash": "sh",
                "c++": "cpp",
                "javascript": "js",
                "js": "js",
                "md": "markdown",
                "py": "python3",
                "python": "python3",
                "shell": "sh",
                "tsx": "typescript-jsx",
                "yml": "yaml",
                "zsh": "sh",
            }
            source_language = manager.get_language(aliases.get(language.lower(), language.lower()))
            if source_language:
                source_buffer.set_language(source_language)
                source_buffer.set_highlight_syntax(True)
            view: Gtk.TextView = GtkSource.View.new_with_buffer(source_buffer)
            self._source_buffers[view] = source_buffer
            # The final scheme is selected from the themed window once this
            # MarkdownView is mounted. This initial sync supplies a safe dark
            # default for isolated renderer tests.
            self._sync_source_color_scheme()
        else:  # pragma: no cover - exercised only on hosts without GtkSource
            source_buffer = Gtk.TextBuffer()
            source_buffer.set_text(code)
            view = Gtk.TextView.new_with_buffer(source_buffer)

        direction_tag = source_buffer.create_tag(
            None,
            direction=Gtk.TextDirection.LTR,
            justification=Gtk.Justification.LEFT,
        )
        source_buffer.apply_tag(
            direction_tag, source_buffer.get_start_iter(), source_buffer.get_end_iter()
        )
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        if GtkSource is not None and isinstance(view, GtkSource.View):
            view.set_show_line_numbers(False)
        # Code fences are part of the document, not nested scroll regions.
        # Wrapping long lines and letting the TextView expose its full natural
        # height keeps wheel/drag scrolling owned by the transcript.
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_hexpand(True)
        view.set_vexpand(False)
        line_count = max(1, code.count("\n") + 1)
        view.set_size_request(
            260,
            max(42, line_count * CODE_LINE_HEIGHT + CODE_VERTICAL_PADDING),
        )
        view.set_justification(Gtk.Justification.LEFT)
        view.set_left_margin(10)
        view.set_right_margin(10)
        view.set_top_margin(9)
        view.set_bottom_margin(9)
        view.set_direction(Gtk.TextDirection.LTR)
        view.get_style_context().add_class("code-content")
        self._register_selection_widget(view)
        outer.pack_start(view, False, False, 0)
        return outer
