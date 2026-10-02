"""Protocol controller for nested side threads.

Side threads use ordinary Codex app-server threads, but their messages and live
state never enter the parent room timeline or sidebar.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple


class BranchController:
    def __init__(
        self,
        store: Any,
        bridge_provider: Callable[[], Any],
        loaded_threads: Set[str],
        on_error: Callable[[str, str], None],
    ) -> None:
        self.store = store
        self.bridge_provider = bridge_provider
        self.loaded_threads = loaded_threads
        self.on_error = on_error
        self.turns: Dict[int, Dict[str, Any]] = {}
        self.panels: Dict[int, Any] = {}

    @property
    def bridge(self) -> Any:
        return self.bridge_provider()

    def register_panel(self, branch_id: int, panel: Any) -> None:
        self.panels[int(branch_id)] = panel
        state = self.turns.get(int(branch_id))
        if state:
            state["panel"] = panel
            panel.set_busy(True)
            panel.set_answer(state.get("final_text") or state.get("streaming_text") or "")

    def unregister_panel(self, branch_id: int, panel: Any) -> None:
        branch_id = int(branch_id)
        if self.panels.get(branch_id) is panel:
            self.panels.pop(branch_id, None)
        state = self.turns.get(branch_id)
        if state and state.get("panel") is panel:
            state["panel"] = None

    def ask(
        self,
        branch_id: int,
        question: str,
        panel: Any,
        model: str,
        effort: Optional[str],
        cwd: str,
    ) -> bool:
        branch_id = int(branch_id)
        question = question.strip()
        if not question or branch_id in self.turns:
            return False
        branch = self.store.get_branch(branch_id)
        if not branch:
            self.on_error("Side thread unavailable", "The side thread no longer exists.")
            return False
        self.store.add_branch_message(branch_id, "user", question)
        panel.add_message("user", question)
        panel.start_answer()
        thread_id = branch.get("thread_id")
        state = {
            "branch_id": branch_id,
            "thread_id": str(thread_id) if thread_id else None,
            "turn_id": None,
            "question": question,
            "quote": str(branch.get("selected_quote") or ""),
            "model": model,
            "effort": effort,
            "cwd": cwd,
            "streaming_text": "",
            "final_text": "",
            "token_usage": None,
            "started_at": time.monotonic(),
            "panel": panel,
        }
        self.turns[branch_id] = state
        self.register_panel(branch_id, panel)
        if not thread_id:
            self.bridge.start_thread(
                model,
                cwd,
                lambda result, bid=branch_id: self._thread_started(bid, result),
                lambda error, bid=branch_id: self._failed(bid, error),
                branch_id=branch_id,
            )
        elif str(thread_id) not in self.loaded_threads:
            self.bridge.resume_thread(
                str(thread_id),
                model,
                cwd,
                lambda result, bid=branch_id: self._thread_resumed(bid, result),
                lambda error, bid=branch_id: self._failed(bid, error),
                branch_id=branch_id,
            )
        else:
            self._start_turn(branch_id)
        return True

    def _thread_started(self, branch_id: int, result: Dict[str, Any]) -> bool:
        state = self.turns.get(branch_id)
        thread_id = (result.get("thread") or {}).get("id")
        if not state or not thread_id:
            return self._failed(branch_id, {"message": "Codex did not return a thread ID."})
        state["thread_id"] = str(thread_id)
        self.store.set_branch_thread_id(branch_id, str(thread_id))
        self.loaded_threads.add(str(thread_id))
        self._start_turn(branch_id, include_quote=True)
        return False

    def _thread_resumed(self, branch_id: int, result: Dict[str, Any]) -> bool:
        state = self.turns.get(branch_id)
        if not state:
            return False
        thread_id = (result.get("thread") or {}).get("id") or state.get("thread_id")
        if not thread_id:
            return self._failed(branch_id, {"message": "The side thread could not be resumed."})
        state["thread_id"] = str(thread_id)
        self.loaded_threads.add(str(thread_id))
        self._start_turn(branch_id)
        return False

    def _start_turn(self, branch_id: int, include_quote: bool = False) -> None:
        state = self.turns.get(branch_id)
        if not state or not state.get("thread_id"):
            return
        prompt = state["question"]
        if include_quote:
            prompt = (
                "This is a focused side thread about a selected passage from an earlier "
                "Codex answer. Keep the response scoped to the question.\n\n"
                f"Selected passage:\n> {state['quote']}\n\nQuestion:\n{prompt}"
            )
        self.bridge.start_turn(
            state["thread_id"],
            prompt,
            state["model"],
            state["effort"],
            state["cwd"],
            None,
            lambda result, bid=branch_id: self._turn_accepted(bid, result),
            lambda error, bid=branch_id: self._failed(bid, error),
            branch_id=branch_id,
        )

    def _turn_accepted(self, branch_id: int, result: Dict[str, Any]) -> bool:
        state = self.turns.get(branch_id)
        if state:
            state["turn_id"] = (result.get("turn") or {}).get("id")
        return False

    def _restore_orphaned_turn(
        self, branch_id: int, thread_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Rebuild a side-thread turn while replaying events after GUI reopen.

        The broker deliberately outlives the window.  A user question is
        written before its app-server request, so a branch whose newest stored
        message is still from the user is an unambiguous pending turn.  This
        mirrors the main-room recovery path while keeping branch traffic out of
        the parent timeline.
        """
        branch_id = int(branch_id)
        existing = self.turns.get(branch_id)
        if existing:
            return existing
        branch = self.store.get_branch(branch_id)
        if not branch:
            return None
        messages = self.store.list_branch_messages(branch_id)
        if not messages or str(messages[-1].get("role") or "") != "user":
            return None
        room = self.store.get_room(int(branch["room_id"]))
        if not room:
            return None
        panel = self.panels.get(branch_id)
        state: Dict[str, Any] = {
            "branch_id": branch_id,
            "thread_id": str(thread_id or branch.get("thread_id") or "") or None,
            "turn_id": None,
            "question": str(messages[-1].get("content") or ""),
            "quote": str(branch.get("selected_quote") or ""),
            "model": str(room.get("model") or ""),
            "effort": room.get("effort"),
            "cwd": str(Path.home()),
            "streaming_text": "",
            "final_text": "",
            "token_usage": None,
            "started_at": time.monotonic(),
            "panel": panel,
        }
        self.turns[branch_id] = state
        if panel:
            panel.start_answer()
        return state

    def _state_for_event(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        turn = params.get("turn") or {}
        item = params.get("item") or {}
        thread_id = params.get("threadId") or turn.get("threadId") or item.get("threadId")
        turn_id = params.get("turnId") or turn.get("id") or item.get("turnId")
        for state in self.turns.values():
            if turn_id and state.get("turn_id") == turn_id:
                return state
            if thread_id and state.get("thread_id") == str(thread_id):
                if not state.get("turn_id") and turn_id:
                    state["turn_id"] = turn_id
                if not turn_id or not state.get("turn_id") or state.get("turn_id") == turn_id:
                    return state
        if thread_id:
            branch = self.store.get_branch_by_thread_id(str(thread_id))
            if branch:
                state = self._restore_orphaned_turn(
                    int(branch["id"]), str(thread_id)
                )
                if state and turn_id:
                    state["turn_id"] = turn_id
                return state
        return None

    def handle_event(self, method: str, params: Dict[str, Any]) -> bool:
        if method == "backend/orphanResponse":
            context = params.get("context") or {}
            branch_id = context.get("branchId")
            if not branch_id:
                return False
            branch_id = int(branch_id)
            if not self._restore_orphaned_turn(branch_id):
                return False
            if params.get("error"):
                self._failed(branch_id, params["error"])
            elif params.get("requestMethod") == "thread/start":
                self._thread_started(branch_id, params.get("result") or {})
            elif params.get("requestMethod") == "thread/resume":
                self._thread_resumed(branch_id, params.get("result") or {})
            elif params.get("requestMethod") == "turn/start":
                self._turn_accepted(branch_id, params.get("result") or {})
            return True

        state = self._state_for_event(params)
        if not state:
            return False
        if method == "item/agentMessage/delta":
            state["streaming_text"] += str(params.get("delta") or "")
            if state.get("panel"):
                state["panel"].set_answer(state["streaming_text"])
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                state["final_text"] = str(item.get("text") or state["streaming_text"])
                if state.get("panel"):
                    state["panel"].set_answer(state["final_text"])
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            state["token_usage"] = int((usage.get("last") or {}).get("totalTokens") or 0)
        elif method == "turn/completed":
            error = (params.get("turn") or {}).get("error") or {}
            self._finish(int(state["branch_id"]), error.get("message"))
        elif method == "error":
            error = params.get("error") or {}
            self._finish(int(state["branch_id"]), error.get("message") or "Codex returned an error.")
        # Consume every event routed to a branch so it cannot leak into a room.
        return True

    def _failed(self, branch_id: int, error: Dict[str, Any]) -> bool:
        if error.get("transient"):
            state = self.turns.get(branch_id)
            panel = (state or {}).get("panel") or self.panels.get(branch_id)
            if panel:
                panel.set_busy(True)
            return False
        self._finish(branch_id, str(error.get("message") or error))
        return False

    def _finish(self, branch_id: int, error_text: Optional[str] = None) -> None:
        state = self.turns.pop(branch_id, None)
        if not state:
            return
        answer = state.get("final_text") or state.get("streaming_text") or ""
        if error_text:
            answer = f"Error: {error_text}"
        if not answer:
            answer = "No response was received. Please try again."
        duration = max(0.0, time.monotonic() - float(state["started_at"]))
        self.store.add_branch_message(
            branch_id,
            "assistant",
            answer,
            duration_seconds=duration,
            token_usage=state.get("token_usage"),
        )
        panel = state.get("panel") or self.panels.get(branch_id)
        if panel:
            panel.finish_answer(answer)
        if error_text:
            self.on_error("Side thread failed", str(error_text))
