# Workspace features

Codex LAN makes a host's coding assistant available to approved coworkers while
keeping the host's provider sign-in private. Start with the [installation and
connection guide](../getting-started/README.md); this page describes the experience.

## One workspace, independent work

Each room has its own conversation, model, thinking level, response speed,
working directory, and read-only preference. **Send update** applies supported
changes to a running response. File attachments are supported, not just images.
Stable pins stay in place; unpinned rooms follow recent messages. First-message
titles, saved drafts, and unread marks make returning to work easier.

Select a passage to start a **Side thread** with independent controls, or open a
floating **More details** explanation. Side threads can be resized; details can
be moved and resized. Both persist across reloads until explicitly closed.

## An inspectable record

**Activity** preserves tool steps and bounded output. **Progress log** retains
public updates after the final answer and after reloads. **Prepared context**
shows available input/context records, not hidden reasoning. These records are
private room data; older runs may lack archived updates.

Per-response token usage and account-share estimates are different measurements.
Missing usage is not silently counted as zero. Administrators can review device
usage, finite allocations, and access controls; estimates are not official billing.

## Attention and navigation

The sidebar profile menu contains My Usage, Easy sign-in, the keyboard guide,
and Administration when authorized. No permanent shortcut badges are needed.

| Shortcut | Action |
| --- | --- |
| `Ctrl+K` / `Cmd+K` | Quick Switcher |
| `Ctrl+/` / `Cmd+/` | Keyboard guide |
| `Ctrl+Alt+PageUp` / `PageDown` | Previous / next displayed room |
| `Ctrl+Alt+S` | Cycle supported response speeds |
| `Ctrl+Alt+R` | Toggle the draft's read-only preference |

Navigation uses physical browser key positions so non-English layouts work too.
Other text fields, open dialogs, and AltGr retain their own key handling. During
a response, read-only changes still require **Send update**.

Reply notifications account for the active room and browser focus. Entering the
room dismisses its notification; marking messages read also requires a visible,
focused page. Ordinary dialogs close on outside clicks; persistent side-thread
and details panels keep explicit close controls.

## International content, restrained polish

The UI, comments, and public docs are English. Message and draft direction uses
Unicode bidirectional character classes rather than language guesses: Arabic,
Persian, Urdu, Hebrew, Syriac, Thaana, NKo, Adlam, and other supported RTL scripts.
Code, math, and password fields stay LTR. Vazir covers Arabic-script typography;
other scripts use system/browser fallbacks. This is not full UI translation.

Cosmic and editor-inspired themes, send effects, and small interaction details
respect reduced-motion preferences. Markdown and math rendering work offline
with bundled browser assets.

## Connected, without relocating your data

Use a trusted LAN directly, or an administrator-provisioned SSH relay to reach a
laptop elsewhere. The laptop keeps the app, history, and provider credentials.
Windows browsers can enroll directly; local file tools currently use the Linux
Connector in WSL2 Ubuntu x86_64. [Platform boundaries](../getting-started/DEPENDENCIES.md).

## Context and compaction

Normal turns send the current message and attachments, not a duplicate of the
visible history or Prepared context display. New and resumed turns use a bounded
96,000-token budget for new content after the retained prefix, with a 12,000-token
tool-output budget. The model's own context limit remains authoritative.
These limits balance context growth and compaction pauses, not promise a fixed
duration or lower usage. Historical recorded limits remain unchanged.
