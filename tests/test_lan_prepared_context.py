"""CODEX: Prepared-context privacy, lifecycle, and durable history regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import test_lan_server as fixture
from codex_lan.runtime import (
    MAX_CONTEXT_IMAGES,
    MAX_CONTEXT_CONTENT_TOTAL,
    MAX_CONTEXT_HISTORY_MESSAGES,
    MAX_CONTEXT_HISTORY_TEXT,
    MAX_CONTEXT_INSTRUCTION_CONTENT_FILES,
    MAX_CONTEXT_INSTRUCTION_TEXT,
    MAX_CONTEXT_INPUT_CHARACTERS,
    MAX_CONTEXT_INPUT_LINES,
    MAX_CONTEXT_LIMIT_TOKENS,
    MAX_CONTEXT_INSTRUCTION_FILES,
    Peer,
    Runtime,
    THREAD_CONTEXT_CONFIG,
    _instruction_content_blocks,
    _new_context_overview,
    _safe_context_overview,
    _safe_instruction_sources,
    _safe_token_count,
)


class PreparedContextWorker(fixture.FakeWorker):
    def __init__(self, ws, name, instruction_sources):
        self.instruction_sources = instruction_sources
        super().__init__(ws, name)

    async def serve(self):
        async for message in self.ws:
            if message.type != fixture.WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if packet.get("type") != "rpc":
                continue
            self.calls.append(packet)
            method = packet["method"]
            if method in {"thread/start", "thread/resume"}:
                result = {
                    "thread": {"id": self.thread},
                    "instructionSources": self.instruction_sources,
                }
            elif method == "turn/start":
                self.next_turn += 1
                self.turn = f"turn-{self.name}-{self.next_turn}"
                result = {"turn": {"id": self.turn}}
            elif method == "lan/upload":
                result = {"path": "/fake/client/upload.png"}
            else:
                result = {}
            await self.ws.send_json({"type": "result", "id": packet["id"], "result": result})


class PreparedContextShapeTests(unittest.TestCase):
    def test_context_budget_counts_new_body_and_keeps_native_window_authoritative(self):
        self.assertEqual(THREAD_CONTEXT_CONFIG, {
            "model_auto_compact_token_limit": 96_000,
            "model_auto_compact_token_limit_scope": "body_after_prefix",
            "tool_output_token_limit": 12_000,
        })
        for thread_id in (None, "existing-thread"):
            with self.subTest(resumed=bool(thread_id)):
                overview = _new_context_overview(
                    {"model": "sol", "effort": "max", "speed": "standard"},
                    {"cwd": "/home/project", "thread_id": thread_id},
                    "continue",
                    [],
                    "message",
                )
                self.assertEqual(_safe_context_overview(overview)["limits"], {
                    "new_context_tokens": 96_000,
                    "scope": "body_after_prefix",
                    "tool_output_tokens": 12_000,
                })

    def test_new_and_resumed_overviews_expose_bounded_redacted_turn_input(self):
        secret_prompt = "Review the selected code\napi_key=sk-proj-super-secret-value"
        run = {
            "model": "astra",
            "effort": "ultra",
            "speed": "fast",
            "tool_read_only": True,
        }
        for thread_id, expected_mode in ((None, "new"), ("private-thread-id", "resumed")):
            with self.subTest(expected_mode):
                room = {"cwd": "/home/private/customer/project", "thread_id": thread_id}
                overview = _new_context_overview(
                    run,
                    room,
                    secret_prompt,
                    [str(index) for index in range(MAX_CONTEXT_IMAGES + 4)],
                    "selected_parent_context",
                )
                safe = _safe_context_overview(overview)
                self.assertEqual(safe["thread"]["mode"], expected_mode)
                self.assertEqual(safe["thread"]["history_managed_by"], "codex")
                self.assertIs(safe["thread"]["exact_prompt_available"], False)
                self.assertEqual(safe["input"]["characters"], len(secret_prompt))
                self.assertEqual(safe["input"]["lines"], 2)
                self.assertEqual(safe["input"]["images"], MAX_CONTEXT_IMAGES)
                self.assertEqual(safe["input"]["source"], "selected_parent_context")
                self.assertTrue(safe["input"]["truncated"])
                self.assertEqual(safe["turn"]["workspace"], "project")
                turn = safe["content"][0]
                self.assertEqual(turn["kind"], "turn_input")
                self.assertEqual(turn["source"], "selected_parent_context")
                self.assertIn("Review the selected code", turn["text"])
                self.assertIn("api_key=[redacted]", turn["text"])
                rendered = json.dumps(safe)
                self.assertNotIn("sk-proj-super-secret-value", rendered)
                self.assertNotIn("/home/private", rendered)
                self.assertNotIn("private-thread-id", rendered)

    def test_instruction_sources_are_counted_but_only_bounded_basenames_survive(self):
        sources = [
            "/home/private/AGENTS.md",
            r"C:\private\SKILL.md",
            "/another/private/AGENTS.md",
            {"prompt": "RAW PROMPT CONTENT"},
            {"reasoning": "PRIVATE REASONING"},
        ] + [f"/home/private/rules-{index}.md" for index in range(30)]
        safe = _safe_instruction_sources(sources)
        self.assertEqual(safe["count"], 33)
        self.assertEqual(safe["files"][:2], ["AGENTS.md", "SKILL.md"])
        self.assertEqual(len(safe["files"]), MAX_CONTEXT_INSTRUCTION_FILES)
        self.assertTrue(safe["truncated"])
        self.assertTrue(all("/" not in name and "\\" not in name for name in safe["files"]))
        rendered = json.dumps(safe)
        for hidden in (
            "/home/private",
            r"C:\private",
            "RAW PROMPT CONTENT",
            "PRIVATE REASONING",
        ):
            self.assertNotIn(hidden, rendered)

    def test_instruction_contents_require_owned_local_regular_text_files(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            instruction = root / "AGENTS.md"
            instruction.write_text(
                "Use the repository tests.\napi_key=instruction-secret-value",
                encoding="utf-8",
            )
            linked = root / "LINKED.md"
            linked.symlink_to(instruction)
            binary = root / "BINARY.md"
            binary.write_bytes(b"rules\x00hidden")
            peer = Peer("local", None, execution_mode="server", filesystem_local=True)
            blocks = _instruction_content_blocks(
                [str(instruction), str(linked), str(binary), "/etc/passwd"], peer
            )
            remote = Peer(
                "remote", None, execution_mode="local", filesystem_local=False
            )
            remote_blocks = _instruction_content_blocks([str(instruction)], remote)
        self.assertEqual(len(blocks), MAX_CONTEXT_INSTRUCTION_CONTENT_FILES)
        self.assertEqual(blocks[0]["label"], "AGENTS.md")
        self.assertIn("Use the repository tests.", blocks[0]["text"])
        self.assertNotIn("instruction-secret-value", json.dumps(blocks))
        for block in blocks[1:]:
            self.assertIn("Content unavailable", block["text"])
        rendered = json.dumps(blocks)
        self.assertNotIn(str(root), rendered)
        self.assertNotIn("/etc/passwd", rendered)
        self.assertEqual(len(remote_blocks), 1)
        self.assertIn("Content unavailable", remote_blocks[0]["text"])
        self.assertNotIn("Use the repository tests.", remote_blocks[0]["text"])

    def test_content_schema_rejects_unknown_sources_and_enforces_aggregate_bounds(self):
        overview = _new_context_overview(
            {"model": "sol", "effort": "medium", "speed": "standard"},
            {"cwd": "/home/private/project", "thread_id": None},
            "x" * (MAX_CONTEXT_CONTENT_TOTAL * 2),
            [],
            "message",
        )
        overview["content"].extend([
            {
                "kind": "instruction",
                "label": "AGENTS.md",
                "source": "app_server_instruction_source",
                "text": "i" * (MAX_CONTEXT_INSTRUCTION_TEXT * 2),
                "truncated": False,
            },
            {
                "kind": "hidden_reasoning",
                "label": "Do not retain",
                "source": "provider",
                "text": "PRIVATE PROVIDER CONTENT",
                "truncated": False,
            },
        ])
        safe = _safe_context_overview(overview)
        self.assertLessEqual(sum(len(block["text"]) for block in safe["content"]),
                             MAX_CONTEXT_CONTENT_TOTAL)
        self.assertTrue(safe["content"][0]["truncated"])
        self.assertTrue(safe["content"][1]["truncated"])
        self.assertNotIn("PRIVATE PROVIDER CONTENT", json.dumps(safe))

    def test_model_context_window_accepts_only_bounded_integer_values(self):
        for value in (0, 128_000, "256000", 512_000.0):
            with self.subTest(value=value):
                self.assertEqual(_safe_token_count(value), int(value))
        for value in (True, -1, 1.5, float("inf"), "bad", {}, [], (1 << 63)):
            with self.subTest(value=value):
                self.assertIsNone(_safe_token_count(value))

    def test_clipped_input_counts_are_explicitly_marked_and_preserved(self):
        overview = _new_context_overview(
            {"model": "sol", "effort": "medium", "speed": "standard"},
            {"cwd": "/home/private/project", "thread_id": None},
            "\n" * (MAX_CONTEXT_INPUT_CHARACTERS + 2),
            [str(index) for index in range(MAX_CONTEXT_IMAGES + 1)],
            "message",
        )
        self.assertEqual(overview["input"]["characters"], MAX_CONTEXT_INPUT_CHARACTERS)
        self.assertEqual(overview["input"]["lines"], MAX_CONTEXT_INPUT_LINES)
        self.assertEqual(overview["input"]["images"], MAX_CONTEXT_IMAGES)
        self.assertIs(overview["input"]["truncated"], True)
        self.assertIs(_safe_context_overview(overview)["input"]["truncated"], True)

    def test_historical_context_limits_are_preserved_when_configuration_changes(self):
        overview = _new_context_overview(
            {"model": "sol", "effort": "medium", "speed": "standard"},
            {"cwd": "/home/private/project", "thread_id": None},
            "hello",
            [],
            "message",
        )
        overview["limits"].update(new_context_tokens=64_000, tool_output_tokens=8_000)
        safe = _safe_context_overview(overview)
        self.assertEqual(safe["limits"]["new_context_tokens"], 64_000)
        self.assertEqual(safe["limits"]["tool_output_tokens"], 8_000)
        for invalid in (0, -1, MAX_CONTEXT_LIMIT_TOKENS + 1):
            with self.subTest(invalid=invalid):
                changed = json.loads(json.dumps(overview))
                changed["limits"]["new_context_tokens"] = invalid
                self.assertIsNone(_safe_context_overview(changed))


class LanPreparedContextHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def context_worker(self, device, name, instruction_sources):
        ws, auth = await fixture.host_worker_socket(
            self.client, self.service, self.admin, device
        )
        self.assertEqual(auth["type"], "auth")
        await ws.send_json({
            "type": "ready",
            "models": fixture.MODELS,
            "cwd": "/home/" + name,
        })
        worker = PreparedContextWorker(ws, name, instruction_sources)
        self.workers.append(worker)
        await self.until(lambda: self.service.runtime.connected(device["device"]["id"]))
        return worker

    async def test_context_persists_through_completion_reload_and_pagination(self):
        secret_path = "/home/private/customer/AGENTS.md"
        sources = [
            secret_path,
            r"C:\private\SKILL.md",
            "/other/private/AGENTS.md",
            {"prompt": "RAW PROMPT CONTENT"},
            {"reasoning": "PRIVATE REASONING"},
        ]
        worker = await self.context_worker(self.a, "prepared", sources)
        room = await self.room(self.a)
        owner_id = self.a["device"]["id"]
        prior = self.service.store.create_run(
            owner_id, room["id"], "Earlier visible user question", [], "prior-context"
        )
        self.service.store.finish_run(
            prior["run"]["id"], "completed", "Earlier visible assistant answer"
        )
        current_prompt = (
            "Please continue this visible review.\n"
            "api_key=sk-proj-current-turn-secret"
        )
        created = await self.ready_run(
            self.a, worker, room, text=current_prompt
        )
        run_id = created["run"]["id"]
        live = self.service.runtime.runs[run_id]
        await self.until(lambda: live.context_overview["instructions"]["count"] == 3)
        start = next(call for call in worker.calls if call["method"] == "thread/start")
        turn = next(call for call in worker.calls if call["method"] == "turn/start")
        self.assertEqual(turn["params"]["input"], [{"type": "text", "text": current_prompt}])
        self.assertEqual(live.context_overview["limits"]["new_context_tokens"],
                         start["params"]["config"]["model_auto_compact_token_limit"])

        compacted = {
            "id": "compact-one",
            "type": "contextCompaction",
            "status": "completed",
            "summary": "RAW COMPACTION SUMMARY",
            "encryptedContent": "RAW COMPACTION PAYLOAD",
            "reasoning": "PRIVATE REASONING",
        }
        await worker.event("item/completed", item=compacted)
        await worker.event("item/completed", item=compacted)
        await worker.event("item/completed", item={**compacted, "id": "compact-two"})
        counts = {
            "inputTokens": 2_400,
            "cachedInputTokens": 800,
            "outputTokens": 320,
            "totalTokens": 2_720,
        }
        await worker.event(
            "thread/tokenUsage/updated",
            tokenUsage={"total": counts, "last": counts, "modelContextWindow": 128_000},
        )
        await self.until(
            lambda: live.context_overview.get("usage", {}).get("model_context_window")
            == 128_000
            and live.context_overview["compaction"]["events"] == 2
        )
        await worker.delta("Prepared context persisted.")
        await worker.complete()
        await self.until(
            lambda: self.service.store.get_run_internal(run_id)["status"] == "completed"
        )

        data = await self.request("GET", f"/api/rooms/{room['id']}/messages", self.a)
        answer = next(
            message
            for message in data["messages"]
            if message["role"] == "assistant" and message["run_id"] == run_id
        )
        overview = answer["context_overview"]
        self.assertEqual(overview["thread"]["mode"], "new")
        self.assertEqual(overview["instructions"], {
            "count": 3,
            "files": ["AGENTS.md", "SKILL.md"],
            "truncated": True,
        })
        self.assertEqual(overview["compaction"], {"events": 2, "contents": "opaque"})
        self.assertEqual(overview["usage"]["model_context_window"], 128_000)
        self.assertEqual(overview["usage"]["cached_input_tokens"], 800)
        self.assertEqual(overview["turn"]["workspace"], "project")
        blocks = overview["content"]
        turn = next(block for block in blocks if block["kind"] == "turn_input")
        history = next(
            block for block in blocks if block["kind"] == "visible_room_history"
        )
        instructions = [block for block in blocks if block["kind"] == "instruction"]
        self.assertIn("Please continue this visible review.", turn["text"])
        self.assertNotIn("current-turn-secret", turn["text"])
        self.assertIn("Earlier visible user question", history["text"])
        self.assertIn("Earlier visible assistant answer", history["text"])
        self.assertNotIn("Please continue this visible review.", history["text"])
        self.assertIn("reference only", history["label"])
        self.assertIn("not a serialized provider prompt", history["label"])
        self.assertEqual(len(instructions), 3)
        self.assertTrue(all("Content unavailable" in block["text"] for block in instructions))
        rendered = json.dumps(overview)
        for hidden in (
            secret_path,
            r"C:\private",
            "RAW PROMPT CONTENT",
            "RAW COMPACTION SUMMARY",
            "RAW COMPACTION PAYLOAD",
            "PRIVATE REASONING",
            "current-turn-secret",
        ):
            self.assertNotIn(hidden, rendered)
        events = self.service.store.events(self.a["device"]["id"], room["id"])
        started = next(event for event in events if event["kind"] == "started")
        summary = next(
            event for event in events if event["kind"] == "activity_summary"
        )
        self.assertIn("content", started["payload"]["context_overview"])
        self.assertNotIn("content", summary["payload"]["context_overview"])
        for event in events:
            payload = event["payload"]
            if event["kind"] in {"context", "usage"}:
                self.assertNotIn("content", payload.get("context_overview", {}))
        all_events = json.dumps(events)
        self.assertNotIn(secret_path, all_events)
        self.assertNotIn("RAW COMPACTION SUMMARY", all_events)
        self.assertNotIn("RAW COMPACTION PAYLOAD", all_events)
        self.assertNotIn("PRIVATE REASONING", all_events)
        self.assertNotIn("current-turn-secret", all_events)
        await self.request("GET", f"/api/rooms/{room['id']}/messages", self.b, expected=403)

        fresh = Runtime(self.service.store, fixture.FakeAuth())
        self.assertEqual(fresh.activity_snapshot(run_id)["context_overview"], overview)

        for index in range(102):
            later = self.service.store.create_run(
                self.a["device"]["id"], room["id"], "Later", [], f"later-context-{index}"
            )
            self.service.store.finish_run(later["run"]["id"], "completed", "Done")
        snapshots = self.service.runtime.snapshots(
            self.a["device"]["id"], room["id"], include_completed_activity=False
        )
        self.assertNotIn(run_id, [run["id"] for run in snapshots])
        before = ""
        seen = None
        for _ in range(7):
            suffix = f"?before={before}" if before else ""
            page = await self.request(
                "GET", f"/api/rooms/{room['id']}/messages{suffix}", self.a
            )
            seen = next(
                (
                    message
                    for message in page["messages"]
                    if message["role"] == "assistant" and message["run_id"] == run_id
                ),
                None,
            )
            if seen or not page["messages"]:
                break
            before = page["messages"][0]["id"]
        self.assertIsNotNone(seen)
        self.assertEqual(seen["context_overview"], overview)

        with self.service.store.db:
            self.service.store.db.execute(
                "DELETE FROM events WHERE run_id=? AND kind='activity_summary'", (run_id,)
            )
        fallback = Runtime(self.service.store, fixture.FakeAuth()).activity_snapshot(run_id)
        self.assertEqual(fallback["context_overview"], overview)

    async def test_visible_history_is_recent_bounded_and_excludes_current_input(self):
        worker = await self.context_worker(self.a, "bounded-history", [])
        room = await self.room(self.a)
        owner_id = self.a["device"]["id"]
        history_runs = MAX_CONTEXT_HISTORY_MESSAGES // 2 + 2
        for index in range(history_runs):
            prior = self.service.store.create_run(
                owner_id,
                room["id"],
                f"history-user-{index}",
                [],
                f"history-request-{index}",
            )
            self.service.store.finish_run(
                prior["run"]["id"], "completed", f"history-assistant-{index}"
            )
        current = "current-input-must-not-be-history"
        created = await self.ready_run(
            self.a, worker, room, request_id="bounded-current", text=current
        )
        live = self.service.runtime.runs[created["run"]["id"]]
        history = next(
            block for block in live.context_overview["content"]
            if block["kind"] == "visible_room_history"
        )
        self.assertTrue(history["truncated"])
        self.assertNotIn("history-user-0", history["text"])
        self.assertIn(f"history-assistant-{history_runs - 1}", history["text"])
        self.assertNotIn(current, history["text"])
        self.assertLessEqual(len(history["text"]), MAX_CONTEXT_HISTORY_TEXT)
        await worker.complete()
        await self.until(
            lambda: self.service.store.get_run_internal(created["run"]["id"])["status"]
            == "completed"
        )

    async def test_resumed_mode_comes_from_room_thread_without_exposing_identifiers(self):
        worker = await self.context_worker(
            self.a, "resume", ["/home/private/AGENTS.md"]
        )
        room = await self.room(self.a)
        room = self.service.store.update_room(
            self.a["device"]["id"], room["id"], thread_id=worker.thread
        )
        baseline = {
            "inputTokens": 1_000,
            "cachedInputTokens": 200,
            "outputTokens": 100,
            "totalTokens": 1_100,
        }
        self.service.runtime.thread_totals[
            (self.a["device"]["id"], worker.thread)
        ] = baseline
        created = await self.ready_run(self.a, worker, room)
        run_id = created["run"]["id"]
        live = self.service.runtime.runs[run_id]
        await self.until(lambda: bool(live.run.get("turn_id")))
        overview = live.context_overview
        self.assertEqual(overview["thread"]["mode"], "resumed")
        self.assertNotIn(worker.thread, json.dumps(overview))
        resume = next(call for call in worker.calls if call["method"] == "thread/resume")
        self.assertIs(resume["params"]["excludeTurns"], True)
        self.assertEqual(overview["limits"]["new_context_tokens"],
                         resume["params"]["config"]["model_auto_compact_token_limit"])
        total = {
            "inputTokens": 1_300,
            "cachedInputTokens": 250,
            "outputTokens": 140,
            "totalTokens": 1_440,
        }
        last = {
            "inputTokens": 120,
            "cachedInputTokens": 10,
            "outputTokens": 20,
            "totalTokens": 140,
        }
        await worker.event(
            "thread/tokenUsage/updated",
            tokenUsage={"total": total, "last": last, "modelContextWindow": 256_000},
        )
        await self.until(lambda: "usage" in live.context_overview)
        self.assertEqual(live.context_overview["usage"], {
            "input_tokens": 300,
            "cached_input_tokens": 50,
            "output_tokens": 40,
            "total_tokens": 340,
            "model_context_window": 256_000,
        })
        await worker.complete()
        await self.until(
            lambda: self.service.store.get_run_internal(run_id)["status"] == "completed"
        )

    async def test_malformed_model_context_window_is_omitted_without_raw_value(self):
        worker = await self.context_worker(self.a, "bad-window", [])
        room = await self.room(self.a)
        created = await self.ready_run(self.a, worker, room)
        run_id = created["run"]["id"]
        live = self.service.runtime.runs[run_id]
        counts = {
            "inputTokens": 100,
            "cachedInputTokens": 10,
            "outputTokens": 20,
            "totalTokens": 120,
        }
        malformed = {"raw": "RAW WINDOW SECRET"}
        await worker.event(
            "thread/tokenUsage/updated",
            tokenUsage={"total": counts, "last": counts, "modelContextWindow": malformed},
        )
        await self.until(lambda: "usage" in live.context_overview)
        self.assertNotIn("model_context_window", live.context_overview["usage"])
        usage_events = [
            event
            for event in self.service.store.events(self.a["device"]["id"], room["id"])
            if event["kind"] == "usage"
        ]
        self.assertEqual(len(usage_events), 1)
        self.assertNotIn("RAW WINDOW SECRET", json.dumps(usage_events))
        await worker.complete()
        await self.until(
            lambda: self.service.store.get_run_internal(run_id)["status"] == "completed"
        )


if __name__ == "__main__":
    unittest.main()
