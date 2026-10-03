# Codex LAN — Story

## From a Persian-friendly desktop chat to a shared AI workspace

Codex LAN began with Hamid wanting a better place to talk to his coding assistant. It grew into a private-network workspace with independent conversations, local-computer access, visible execution, usage controls, and a carefully shaped interface.

Hamid conceived and directed the product. Codex was the implementation partner: suggesting designs, writing code, investigating failures, and building tests. The underlying AI and Codex app server come from OpenAI; this project is an independent interface and orchestration layer, not an OpenAI product.

This is the story of that collaboration—not a claim that the first generated version got everything right.

It covers the project's development through early October 2026.

## 1. The original idea: make the assistant comfortable to live with

The starting point was not a team dashboard. It was the friction of using Persian in a terminal: mixed text direction, awkward rendering, and a conversation experience that did not feel like a natural daily workspace.

Hamid's first concrete request combined several ideas: a Python desktop application, an icon to pin to the dock, stacked conversation rooms, a model selector, image input, and access to the computer when appropriate. The assistant should be somewhere he could return to throughout the day, rather than a terminal session he had to manage.

An especially consequential requirement followed: the product needed to survive work on itself. Changing or closing its interface should not automatically destroy the conversation running behind it. That pushed the early desktop application toward a persistent backend separate from the window.

The project later moved into the browser and onto the local network, while retaining an optional legacy desktop client.

| Stage | What changed |
| --- | --- |
| Early September 2026 | Persian-friendly desktop chat, dock integration, independent rooms, and a persistent backend. |
| Following iterations | Richer messages, keyboard workflows, drafts, streaming activity, and concurrent conversations. |
| LAN expansion | A shared Linux host, browser clients, device approval, and native Connectors for client-side work. |
| Product refinement | Read-only turns, steering, contextual side conversations, usage dashboards, themes, and notifications. |
| Operational hardening | Bounded output, asynchronous persistence, guarded deployment, safer recovery, and portable setup. |

The first Git baseline came later than the first working application. The history of the product starts with the conversations, not with the first commit.

## 2. Hamid's defining idea: a room is a workspace, not just a transcript

Hamid repeatedly pushed for conversations to behave independently. One room should keep working while another is opened. Model, thinking level, speed, workspace, and access settings belong to the relevant conversation—not to a global interface that silently changes everything.

This shaped a collection of small but important behaviors:

- Separate personal rooms from other people's rooms; administrators seeing a room should not acquire its owner's unread state.
- Keep pinned rooms in a stable order, while unpinned rooms move up when an actual message arrives—not when someone renames them or changes a setting.
- Create a new conversation immediately, without a naming dialog. Derive its initial title locally from the first prompt; allow explicit renaming afterward.
- Preserve drafts when switching rooms or reconnecting, and keep the currently open room visibly selected even on hover.
- Put page and panel state in the URL so reloading Usage, Administration, or a device profile does not unexpectedly return to the chat screen.
- Allow workspace changes through the interface, with restrictions while an execution is active.

These decisions made the application feel like a set of ongoing places to work, rather than a list of disposable requests.

## 3. Control should be explicit—and truthful

Hamid wanted model selection, speed, thinking, and read-only access close to the message composer. He also wanted wheel and keyboard control, a compact thinking stepper rather than an unattractive native select box, and readable controls even in a narrow side panel.

The details mattered. A response sent with a particular thinking level should still show that level while it runs; it should not suddenly appear as “Default.” Unsupported or duplicated levels should not be disguised with misleading labels. Model discovery and access confirmation should reflect the installed Codex environment, rather than treating a name in the interface as proof of availability.

The read-only toggle was another distinctive requirement: it should mean the assistant cannot change files or the computer through its execution tools, not merely that a warning has been added to the prompt. The implementation uses execution restrictions for that turn. Access can also be changed through **Send update**, alongside instructions that steer an already-running response.

Hamid's workflow rules became persistent instructions too: distinguish changes that need a restart from changes that should wait until the next launch, and support a more cautious editing mode. Codex proposed keeping those rules in `AGENTS.md`, rather than inventing a separate “learn” file.

## 4. Two different ways to explore a selected passage

One of Hamid's most specific interaction designs was the distinction between **Side thread** and **More details**.

Selecting text should produce a small, unobtrusive toolbar only after the mouse is released, with a short delay and a fade. The selection should be copied internally—not written to the system clipboard—and should not fail merely because it crosses message boundaries.

**Side thread** is a separate working conversation shown beside the original. It has independent settings and a mouse-resizable split. The selected passage and bounded room context seed the discussion, while its history and open state persist across reloads and disconnects. Closing it calls for a warning because the user may be discarding an actual conversation.

**More details** serves a different purpose: explain something without taking over half the workspace. It uses a themed, scrollable, draggable, resizable floating panel, with a lower-cost model and low thinking configuration in read-only mode. It remains available until explicitly closed, but does not need the same closing warning as a working side conversation.

The persistence requirements were Hamid's idea. Codex's contribution included the stored lifecycle, bounded context snapshots, and separation of explanation permissions from ordinary execution. Closing a panel removes its application record; it does not retroactively erase independently retained backups.

## 5. Persian-first does not mean code-second

Hamid treated Persian rendering as a core product requirement, not a cosmetic translation task.

That meant preserving natural mixed Persian/English text, making selection work across prose and code, bundling Vazir locally, and keeping code readable in its own direction and font. Markdown, tables, code blocks, and rendered mathematics needed to coexist without exposing raw formatting syntax where a readable result was expected.

The composer was refined through repeated screenshots: cramped controls, overlapping read-only labels, clipped “Standard” text, excessive headers, and awkward split-pane layouts were all challenged. Reply remained a simple message action; side-thread controls moved out of the way instead of crowding every header.

An optional double-Enter send mode addressed accidental submissions. File attachments expanded beyond images, with visible upload state and previews where appropriate. These are not headline features, but they remove friction from everyday use.

## 6. Make the work visible, and keep it visible afterward

Hamid did not want a long-running assistant to display an unexplained “Working” label and then erase its public updates when the answer arrived.

The resulting Activity experience separates summary, steps, output, and a durable **Progress log**. Public updates shown during execution remain attached to the response after completion and reload. A user can inspect what happened without hunting through terminal logs.

He also asked to see the actual context prepared locally, not just an analysis of it. The **Prepared context** view therefore exposes available input content, bounded visible history, permitted local instruction content, and the settings captured for that turn. Codex added the idea of a durable per-response receipt so changing settings later does not rewrite the record of an earlier request.

There is an important boundary: this is not a dump of private reasoning or the exact hidden provider request. Opaque compaction content is marked unavailable. Reported token totals are not presented as if they were the current size of the context window.

## 7. Usage needs a denominator—and an explanation

Hamid pushed usage reporting well beyond a generic percentage bar.

An individual's allocation is a share of the account allowance. The bar should show how much of that assigned share is used, while the text also explains the account-percentage amount. For example, in an illustrative allocation of 8%, estimated use of 2 account-percentage points fills one quarter of that person's bar—not 2% of it.

He also requested an aggregate view for all finite allocations. Unlimited users are outside that pool, so an administrator can see how much of the allowance reserved for the platform has been consumed.

The broader requirements were equally important:

- Attach usage to the individual response, not only to a dashboard.
- Keep reported tokens, estimated cost units, and reconciled account percentages distinguishable.
- Explain input, cached input, output, and the effects of repeated tool/model steps.
- Do not equate a short typed prompt with a necessarily cheap execution.
- Investigate idle charges, duplicate reports, missing reports, and abnormal long-running work.
- Keep usage history when allocations change; do not call unreported usage zero.

Codex implemented attribution, deduplication, reconciliation, and diagnostic checks. These remain application estimates, not official per-user billing measurements. The development record also includes corrections to misleading displays and overrestrictive behavior; those mistakes are part of the story, not evidence that every early safeguard was sound.

## 8. The shared host should not become everybody's computer

The LAN expansion introduced a subtle requirement: a central host can run Codex, but work requested for a client must reach the correct client's files and artifacts.

Hamid wanted people to connect through a browser without exchanging SSH passwords or manually managing a maze of terminal commands. Codex proposed a small native Connector because a browser alone cannot provide arbitrary local filesystem and command access.

The architecture separates the Linux host, browser session, and approved native client. Connectors communicate outward to the host over secure WebSockets. Missing client execution access must not silently redirect another user's work into the administrator's filesystem.

Pairing and updates became a product concern too: versioned installer and pairing names, clearer download commands, persistent pairing across host restarts, and a simpler personal setup flow. Administration moved toward compact rows and expandable profiles rather than walls of technical detail.

For easier login, Hamid asked for a friendlier alternative to copying raw credentials. Codex proposed password-based browser sign-in with hashed passwords and a separate browser session, while retaining distinct Connector credentials instead of weakening them into a convenient shared token.

## 9. Beauty through feedback, not constant distraction

Hamid's visual direction was unusually hands-on. He asked for a ChatGPT-like dark theme with an IntelliJ-inspired feel, a Vim Torte theme, and the atmospheric Cosmic look. He also rejected designs that were too large, too loud, or too busy.

The continuously animated Cosmic Live experiment was removed at his request. Its replacement direction was more specific: make important actions feel intelligent, but let the rest of the workspace stay calm.

The send interaction developed into a roughly three-second sequence of light, particles, pulses, and a soft message arrival. It needed to work from Enter as well as a mouse click, without disappearing because a fast local HTTP response ended the sending state before a browser frame was drawn.

Hamid also specified subtle hover-revealed room actions, a rightmost pin button, theme-consistent danger styling, an active-room marker that survives hover, and a glowing blue unread dot for the actual owner.

Codex supplied the concrete visual treatments: staged send particles and waves, copy confirmation, upload transitions, completion glows, gentle Activity expansion, and restrained model/thinking feedback. Reduced-motion support keeps those effects optional at the operating-system preference level.

## 10. Notifications should understand attention

Hamid's notification rule was not simply “notify when complete.”

If the user is reading that room in a focused browser, remain quiet. If they are elsewhere—or the same room is open but the browser is not focused—notify them. Keep the notification available rather than dismissing it on a timer, and close it when they return to the relevant room with focus.

Codex implemented the focus, visibility, and cross-tab coordination behind that behavior. Browser permission is still required, and this is not a promise of background push delivery after every application tab has been closed.

## 11. Reliability was learned through uncomfortable failures

The application was developed while it was also being used. That exposed real failures: huge command output, expensive text cleanup, tiny streamed fragments causing repeated work, synchronous database operations blocking network handling, and recovery policies that could interrupt legitimate responses.

Some failures came from Codex's own diagnostic or implementation choices. They should not be rewritten as an effortless success story.

Hamid's repeated requirement was clear: stop making people restart the host just to keep working, and do not “repair” a connection by killing a healthy prompt.

The engineering response included bounded tool output, cooperative event processing, delta batching, background persistence, better database queries, reconnect diagnostics, and safer request retry handling. Deployment gained active-run checks, health verification, and rollback. Recovery needed to distinguish a crashed process from a busy or unresponsive one; an `active` service label alone was not enough.

The project also distinguishes locking the desktop from suspending the host. Host setup can help keep the service available, but no application can serve LAN clients while the computer is actually asleep.

These are implemented safeguards and tested failure paths—not a claim that future disconnects are impossible.

Verification grew to include unit tests, real-browser checks, streaming stress cases, deliberate database delays, and publication scans. A recurring lesson was that a fix in the source tree is not enough: the installed version and its behavior must be verified too.

## 12. Distinctive ideas contributed by Codex

Not every useful addition started as an explicit feature request. The clearest Codex-origin proposals in the development record include:

| Codex proposal or concrete design | Why it mattered |
| --- | --- |
| Persistent workflow rules in `AGENTS.md` | Made Hamid's preferred editing and restart behavior reusable. |
| A keyboard Quick Switcher with `Ctrl+K` / `Cmd+K` | Added fast room navigation and access to common destinations. |
| A local draft-size and approximate-token meter | Gave feedback before sending without making an extra model request. |
| Early room-local drafts and attachment feedback | Reduced lost work and uncertainty while switching conversations. |
| A per-response context receipt | Preserved the settings and available context of the actual execution. |
| Separate browser sign-in and Connector credentials | Made login friendlier without turning execution credentials into a weak shared password. |
| Staged send effects and smaller interaction feedback | Turned Hamid's visual direction into a consistent motion language. |
| Guarded activation and failure-focused regression tests | Converted recurring incidents into explicit engineering checks. |

Several of these were co-designed: Hamid supplied the problem, constraints, and acceptance criteria; Codex proposed the mechanism. The goal of this credit is accuracy, not assigning every feature to a single author.

## 13. Ready to move, without taking private life into the repository

Hamid wanted to move the host to another Linux computer and eventually place the source on GitHub. He also explicitly insisted that existing conversations and user information must not go with it.

That led to a source-only publication export, fresh repository history, publication checks, separate private backups, and local-only summary reports. Runtime databases, uploads, tokens, pairing credentials, certificates, authentication caches, and conversation logs belong outside the source repository.

The setup work brought host diagnostics, network configuration guidance, certificate information, Connector preparation, and optional keep-awake controls into the interface. Privileged operating-system actions still require local approval; the application should not ask people to paste an administrator password into chat.

This remains a trusted-LAN project, not a hardened public-internet or hostile multi-tenant service. Model availability depends on the installed provider environment. No project-wide license grant has been selected.

## What this collaboration demonstrates

Codex LAN was shaped by small, persistent acts of product judgment: a percentage needs the right denominator; a selected passage deserves two different kinds of exploration; a notification should respect attention; a saved log should survive the final answer; and a beautiful animation should not make the whole workspace restless.

Hamid contributed the vision, original interaction ideas, detailed criticism, priorities, and decisions about what stayed or was removed. Codex contributed proposals, implementation, investigation, tests, and specific design treatments. Real use supplied the pressure that neither a feature list nor a first prototype could have provided.

The result is not simply a chat interface generated in one pass. It is a product refined through conversation—and through the willingness to say, repeatedly, that something still was not right.
