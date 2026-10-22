# Codex desktop architecture

The optional GTK desktop application's source lives in `apps/desktop/`.
The shared browser workspace is described in
[the Connector and release guide](../operations/LAN-RELEASES.md).

## Component boundaries

- `app.py` owns the GTK shell, active-room controls, main timelines, selection
  actions, and the live per-room state in `room_turns`.
- `markdown_view.py` safely renders Markdown, resolves bidirectional text at the
  block level, and exposes one message-level selection API across its child
  widgets.
- `branch_view.py` renders a compact private side thread beneath its parent
  answer.
- `branch_controller.py` owns side-thread protocol state and prevents branch
  events from leaking into a room timeline.
- `codex_bridge.py` is the JSON-RPC client. It owns callbacks, request timeouts,
  and socket-close handling.
- `backend.py` owns the long-lived `codex app-server` process and the reconnect
  buffer.
- `storage.py` owns rooms, messages, replies, private branches, drafts,
  per-room settings, and imported files.

## Concurrent-room invariants

1. `room_id` is always the primary key for live GUI state.
2. An event is routed by `turn_id` first and `thread_id` second.
3. An event carrying a different `turn_id` on the same thread is stale and must
   never enter the current turn.
4. Model and reasoning controls represent only the active room. They are locked
   only when that same room owns an active turn; turns in other rooms must not
   disable them.
5. Available effort values come from the selected model's app-server metadata.
   Their stable display order is `none`, `minimal`, `low`, `medium`, `high`,
   `xhigh`, `max`, and `ultra`; unsupported values are not invented. The compact
   UI labels the final three levels `Extra`, `Max`, and `Ultra`, while tooltips
   retain their full meaning. A star marks the recommended/default model.
6. Closing the window does not stop the backend. Events are buffered in arrival
   order until the GUI reconnects.
7. Streaming follows the bottom only while the reader is already near the
   bottom. Manual scrolling and text selection detach follow mode.
8. Each `thread_id` maps to at most one local room, enforced by a partial unique
   index.
9. History initially loads the newest 120 messages. Older pages are prepended
   without moving the visible viewport.

## Message selection, replies, and private branches

`MarkdownView` renders a message as multiple GTK widgets but registers every
selectable prose and code child in document order. Its message-level selection
API strips internal bidirectional controls and returns one coherent excerpt.
Continuous cross-block selection is represented by that aggregate selection;
ordinary pointer drag extends it across child widgets, while holding `Ctrl`
preserves a separate fragment before selecting another child widget.

Selection actions are scoped to one assistant message:

1. **Copy** writes the exact aggregate excerpt to the clipboard.
2. **Reply to selection** records the source message and exact excerpt, then
   shows it above the main composer. Both `reply_to_id` and the exact
   `reply_quote` are persisted; quoted context is added to the outbound prompt
   only when sent.
3. **Ask in side thread** creates a branch record containing `room_id`,
   `parent_message_id`, and `selected_quote`, then mounts a `BranchPanel` beneath
   the answer.

A branch uses its own app-server `thread_id`, messages table, and live controller
state. Branch protocol events are consumed before normal room routing, so they
cannot alter the parent answer, unread state, room timer, or sidebar. Branches
are intentionally absent from the room list: collapsing a panel hides the
focused conversation without deleting it. If the window closes during a side
thread turn, the latest pending branch question plus its persisted thread ID
reconstruct the isolated controller state when broker events are replayed.

## Reconnect ordering

Requests for `thread/start`, `thread/resume`, and `turn/start` carry an internal
context with a `roomId` or `branchId`. The backend never forwards this context to
Codex. If the GUI disconnects before a response arrives, the backend returns it
later as `backend/orphanResponse` with its original context.

Delivery after reconnect must remain:

1. the `initialize` response;
2. buffered events in their original order;
3. newly arriving live events.

Bridge requests have timeouts. Closing the socket rejects every pending
callback. Under queue pressure, the backend never discards terminal turn
events, server requests, or orphan responses.

## Theme contract

Theme classes live on the top-level window and every component uses semantic
style classes rather than fixed widget-local colors. The themes deliberately
have separate palettes:

- `theme-night`: navy background and cool slate surfaces;
- `theme-light`: paper background and pale neutral surfaces;
- `theme-graphite`: charcoal background and a violet branch treatment.

An independent `accent-blue`, `accent-red`, or `accent-green` class controls
primary actions, focus, selection, carets, and active-room indicators.

Each theme must explicitly cover text selection, focus, disabled and hover
states, the sidebar, user and assistant messages, Markdown structures, code,
tables, the composer, model and effort selectors, and private branches.

## Imported images

Images are stored as JSON path lists with the first path mirrored into legacy
single-image columns. A file under `uploads/` is removed only when neither a
persisted message nor any draft references it. Startup cleanup considers only
files inside that directory.

## Pre-install verification

```bash
python3 development/project.py test -v
python3 development/project.py desktop --check
PYTHONPATH=apps/desktop:apps/workspace:development python3 development/tests/visual_smoke.py night /tmp/codex-night.png
PYTHONPATH=apps/desktop:apps/workspace:development python3 development/tests/visual_smoke.py light /tmp/codex-light.png
PYTHONPATH=apps/desktop:apps/workspace:development python3 development/tests/visual_smoke.py graphite /tmp/codex-graphite.png
```
