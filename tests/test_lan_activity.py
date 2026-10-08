"""CODEX: Public activity lifecycle, safe output, and durable refresh regressions."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.activity import (
    ActivityLog,
    MAX_ENTRIES,
    MAX_OUTPUT,
    MAX_RUN_OUTPUT,
    MAX_STREAMED_OUTPUT,
    OUTPUT_OMITTED,
    latest_public_line,
    normalize_item,
    redact,
)
from codex_lan.runtime import LiveRun, Peer, Runtime
from codex_lan.store import Store


def command(identifier="cmd", **fields):
    return {"id": identifier, "type": "commandExecution", "command": "python -m unittest", "cwd": "/work/project",
            "status": "inProgress", **fields}


class ActivityTests(unittest.TestCase):
    def test_public_last_line_keeps_multiline_redaction_context(self):
        self.assertEqual(latest_public_line("Checked one\nChecked two\n"), "Checked two")
        private_marker = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertNotIn("PRIVATE-BODY", latest_public_line(private_marker + "\nPRIVATE-BODY"))
        self.assertLessEqual(len(latest_public_line("x" * 2000)), 501)
        self.assertNotIn("truncated", latest_public_line("x" * 2000))
        self.assertEqual(latest_public_line("Downloading 10%\rDownloading 20%\rDownloading 30%"), "Downloading 30%")
        self.assertNotIn("proxy-password", latest_public_line("Using socks5h://user:proxy-password@proxy.test:8124"))

    def test_trailing_output_flushes_without_another_chunk_and_without_duplicates(self):
        log = ActivityLog()
        log.item(command(), now=1)
        log.output({"itemId": "cmd", "delta": "First\n"}, now=2)
        self.assertIsNone(log.output({"itemId": "cmd", "delta": "Still running\n"}, now=2.1))
        self.assertFalse(log.flush(now=2.2))
        changes = log.flush(now=2.4)
        self.assertEqual(changes[0]["output"], "First\nStill running\n")
        self.assertFalse(log.flush(now=3))
        log.item(command(status="completed"), True, now=4)
        self.assertFalse(log.flush(now=5))

    def test_command_lifecycle_is_upserted_and_completed_output_authoritative(self):
        log = ActivityLog()
        started = log.item(command(), now=10)
        self.assertEqual(started["kind"], "command")
        self.assertEqual(started["status"], "running")
        self.assertIn("/work/project", started["detail"])
        self.assertIsNone(log.item(command(), now=11))
        log.output({"itemId": "cmd", "delta": "first\n"}, now=11)
        complete = command(status="completed", aggregatedOutput="first\nlast\n", exitCode=0, durationMs=182)
        entry = log.item(complete, completed=True, now=12)
        self.assertEqual(entry["output"], "first\nlast\n")
        self.assertEqual(entry["exit_code"], 0)
        self.assertEqual(entry["duration_ms"], 182)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["started_at"], 10)
        self.assertIsNone(log.item(complete, completed=True, now=13))
        self.assertIsNone(log.output({"itemId": "cmd", "delta": "late"}, now=14))
        self.assertEqual(len(log.entries), 1)

    def test_nonzero_exit_failure_and_declined_are_not_success(self):
        for fields, expected in (({"exitCode": 7}, "failed"), ({"status": "failed"}, "failed"),
                                 ({"status": "declined"}, "cancelled")):
            self.assertEqual(normalize_item(command(**fields), True)["status"], expected)
        tool = {"id": "tool", "type": "dynamicToolCall", "tool": "check", "success": False}
        self.assertEqual(normalize_item(tool, True)["status"], "failed")

    def test_duplicate_output_event_id_is_ignored_but_repeated_text_is_valid(self):
        log = ActivityLog()
        log.item(command(), now=1)
        params = {"itemId": "cmd", "delta": "same\n", "eventId": "event-one"}
        log.output(params, now=2)
        self.assertIsNone(log.output(params, now=3))
        log.output({**params, "eventId": "event-two"}, now=4)
        self.assertEqual(log.entries["cmd"]["output"], "same\nsame\n")

    def test_fast_output_is_coalesced_and_flushed_on_completion(self):
        log = ActivityLog()
        log.item(command(), now=1)
        log.output({"itemId": "cmd", "delta": "a"}, now=2)
        self.assertIsNone(log.output({"itemId": "cmd", "delta": "b"}, now=2.1))
        self.assertEqual(log.entries["cmd"]["output"], "a")
        final = log.item(command(status="completed"), True, now=3)
        self.assertEqual(final["output"], "ab")

    def test_command_stream_stops_processing_after_bounded_output(self):
        log = ActivityLog()
        log.item(command(), now=1)
        first = log.output(
            {"itemId": "cmd", "delta": "x" * (MAX_STREAMED_OUTPUT + 100)}, now=2
        )
        self.assertIn(OUTPUT_OMITTED.strip(), first["output"])
        self.assertIsNone(log.output({"itemId": "cmd", "delta": "y" * 1000000}, now=3))
        final = log.item(command(status="completed"), True, now=4)
        self.assertIn(OUTPUT_OMITTED.strip(), final["output"])

    def test_concurrent_steps_keep_real_running_summary(self):
        log = ActivityLog()
        log.item(command("one", command="first command"))
        log.item(command("two", command="second command"))
        self.assertIn("2 active steps", log.snapshot()["activity"])
        log.item(command("two", command="second command", status="completed"), True)
        self.assertEqual(log.snapshot()["activity"], "first command")
        log.item(command("one", command="first command", status="completed"), True)
        self.assertEqual(log.snapshot()["activity"], "Waiting for model")
        self.assertEqual(log.snapshot("completed")["activity"], "Work completed")

    def test_finish_does_not_invent_success_for_running_steps(self):
        log = ActivityLog()
        log.item(command())
        log.finish("completed")
        self.assertEqual(log.entries["cmd"]["status"], "cancelled")
        self.assertIn("not reported", log.entries["cmd"]["detail"])

    def test_reasoning_and_agent_message_never_enter_public_activity(self):
        log = ActivityLog()
        for kind in ("reasoning", "agentMessage", "userMessage", "unknown"):
            self.assertIsNone(log.item({"type": kind, "id": "hidden", "text": "private text", "summary": "private text"}))
        self.assertFalse(log.entries)

    def test_files_and_search_show_concrete_subjects(self):
        log = ActivityLog()
        entry = log.item({"id": "f", "type": "fileChange", "status": "completed",
                          "changes": [{"path": "/work/a.py", "kind": "update", "diff": "+ safe line"}]}, True)
        self.assertEqual(entry["title"], "Edit a.py")
        self.assertEqual(entry["output"], "+ safe line")
        query = log.item({"id": "s", "type": "webSearch", "query": "official app-server docs"})
        self.assertIn("official app-server docs", query["title"])

    def test_dynamic_and_mcp_extract_text_not_binary_or_json(self):
        for kind in ("dynamicToolCall", "mcpToolCall"):
            log = ActivityLog()
            item = {"id": "t", "type": kind, "server": "terminal", "tool": "execute",
                    "arguments": {"cmd": "pwd", "workdir": "/work", "token": "DO-NOT-LOG"},
                    "contentItems": [{"type": "text", "text": '{"output":"2 tests passed","other":"HIDDEN"}'},
                                     {"type": "image", "data": "BINARY-SECRET"}]}
            if kind == "mcpToolCall":
                item["result"] = {"content": item.pop("contentItems")}
            entry = log.item(item, True)
            rendered = json.dumps(entry)
            self.assertIn("pwd", entry["detail"])
            self.assertIn("2 tests passed", entry["output"])
            self.assertNotIn("BINARY-SECRET", rendered)
            self.assertNotIn("DO-NOT-LOG", rendered)
            self.assertNotIn("HIDDEN", rendered)

    def test_wrapper_arguments_show_names_without_javascript(self):
        log = ActivityLog()
        entry = log.item({"id": "wrapper", "type": "dynamicToolCall", "tool": "exec",
                          "arguments": 'const hidden = "SECRET-PROMPT"; await tools.exec_command({cmd: "pwd"});'})
        self.assertEqual(entry["detail"], "Tools: exec_command")
        self.assertNotIn("SECRET-PROMPT", json.dumps(entry))

    def test_redaction_covers_credentials_headers_jwt_and_ansi(self):
        secrets = ["hunter123", "VERYSECRETVALUE", "TOKEN-SECRET", "JWT-SECRET", "URL-PASSWORD"]
        text = ('password="hunter123" API_KEY=VERYSECRETVALUE\nAuthorization: Bearer TOKEN-SECRET\n'
                'sshpass -p "hunter123" ssh host\nhttps://user:URL-PASSWORD@example.com/\n'
                '\x1b[31mred\x1b[0m eyJhbGciOiJIUzI1NiJ9.eyJzZWNyZXQiOiJKV1QtU0VDUkVUIn0.signature')
        safe = redact(text)
        for secret in secrets:
            self.assertNotIn(secret, safe)
        self.assertNotIn("\x1b", safe)
        self.assertIn("redacted", safe)

    def test_redaction_stays_bounded_on_unterminated_control_and_data_prefixes(self):
        self._assert_bounded_redaction(
            'value = ("\\x1b]data:" * 40000)[:262144]\n'
            'assert "\\x1b" not in redact(value, 262144)'
        )

    def _assert_bounded_redaction(self, scenario):
        program = "from codex_lan.activity import redact\n" + scenario
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_redaction_hides_server_managed_remote_worker_paths(self):
        path = ("/home/host/.local/share/codex-lan/server/remote-workers/"
                "f6a0d698-f8a4-4816-b39a-0ee64f92ac21/generated_images/thread/image.png")
        safe = redact("cat '" + path + "'\nDirectory: /home/client")
        self.assertNotIn("/home/host", safe)
        self.assertNotIn("remote-workers", safe)
        self.assertIn("[server-managed runtime]", safe)
        self.assertIn("/home/client", safe)

    def test_runtime_path_redaction_is_linear_on_long_nonmatching_paths(self):
        self._assert_bounded_redaction(
            'value = "/" + "dependency/" * 20000 + "ordinary-workspace"\n'
            'assert "ordinary-workspace" in redact(value, 262144)\n'
            'value = "/" + "(/remote-workers/not-a-uuid/" * 8000\n'
            'assert "not-a-uuid" in redact(value, 262144)'
        )

    def test_jwt_redaction_is_bounded_on_repeated_incomplete_prefixes(self):
        self._assert_bounded_redaction(
            'value = "eyJpayload-" * 24000\n'
            'assert "eyJpayload" in redact(value, 262144)\n'
            'value = "eyJpayload-" * 23000 + ".payload.signature"\n'
            'assert redact(value, 262144) == "[redacted token]"'
        )

    def test_linear_redactors_keep_token_and_path_boundaries(self):
        identifier = "f6a0d698-f8a4-4816-b39a-0ee64f92ac21"
        path = "/remote-workers/" + identifier
        self.assertEqual(redact("(" + path + "/image.png)"), "([server-managed runtime]")
        self.assertEqual(redact("relative" + path), "relative" + path)
        self.assertEqual(redact("//host" + path), "/[server-managed runtime]")
        self.assertEqual(redact("eyJx.payload.signature.extra"), "[redacted token].extra")
        self.assertEqual(redact("prefix-eyJx.payload"), "prefix-[redacted token]")
        self.assertEqual(redact("prefix_eyJx.payload"), "prefix_eyJx.payload")
        self.assertEqual(redact('\u062d\u0631\u0641eyJx.payload'), '\u062d\u0631\u0641eyJx.payload')

    def test_output_and_entry_count_have_hard_bounds(self):
        log = ActivityLog()
        for index in range(MAX_ENTRIES + 20):
            log.item(command(str(index), aggregatedOutput="x" * 100000, status="completed"), True)
        snapshot = log.snapshot()
        self.assertEqual(len(snapshot["activities"]), MAX_ENTRIES)
        self.assertEqual(snapshot["activity_count"], MAX_ENTRIES + 20)
        self.assertTrue(snapshot["activity_truncated"])
        self.assertLessEqual(sum(len(entry.get("output", "")) for entry in snapshot["activities"]), MAX_RUN_OUTPUT)
        for entry in snapshot["activities"]:
            self.assertLessEqual(len(entry.get("output", "")), MAX_OUTPUT)
        self.assertEqual(len(snapshot["activities"][-1]["output"]), MAX_OUTPUT)
        self.assertTrue(log.take_updates())


class RuntimeActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.admin = self.store.bootstrap_admin("Server")
        self.device_id = self.admin["device"]["id"]
        self.room = self.store.create_room(self.device_id, "Room", "/work")
        self.run = self.store.create_run(self.device_id, self.room["id"], "hello", [], "one")["run"]
        self.store.update_run(self.run["id"], status="running", thread_id="thread", turn_id="turn")
        self.run.update(status="running", thread_id="thread", turn_id="turn")
        self.peer = Peer(self.device_id, None, ready=True)
        self.runtime = Runtime(self.store, None)
        self.live = LiveRun(self.run, self.peer)
        self.runtime.runs[self.run["id"]] = self.live
        self.runtime.by_thread[(self.device_id, "thread")] = self.live

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, method, **params):
        self.runtime.event(self.peer, method, {"threadId": "thread", "turnId": "turn", **params})

    def test_runtime_persists_only_sanitized_entries_and_replays_terminal(self):
        self.event("item/started", item=command(command="API_KEY=MY-SECRET pytest"))
        self.event("item/commandExecution/outputDelta", itemId="cmd", delta="3 tests passed\n")
        self.event("item/completed", item=command(command="API_KEY=MY-SECRET pytest", status="completed", exitCode=0,
                                                 aggregatedOutput="3 tests passed\n", durationMs=300))
        self.event("turn/completed", turn={"id": "turn", "status": "completed"})
        all_events = self.store.events(self.device_id, self.room["id"])
        self.assertNotIn("MY-SECRET", json.dumps(all_events))
        self.assertTrue(any(event["kind"] == "activity_summary" for event in all_events))
        fresh = Runtime(self.store, None)
        snapshot = fresh.snapshots(self.device_id, self.room["id"])[0]
        self.assertEqual(snapshot["activities"][0]["output"], "3 tests passed\n")
        self.assertEqual(snapshot["activities"][0]["status"], "completed")
        self.assertEqual(snapshot["activity"], "Work completed")
        self.assertEqual(fresh.activity_snapshot(self.run["id"]), fresh.activity_snapshot(self.run["id"]))
        lightweight = fresh.snapshots(self.device_id, self.room["id"], include_completed_activity=False)[0]
        self.assertNotIn("activities", lightweight)

    def test_runtime_drops_hidden_reasoning_and_keeps_commentary(self):
        self.event("item/started", item={"type": "reasoning", "id": "secret", "text": "RAW-REASONING"})
        self.event("item/reasoning/textDelta", itemId="secret", delta="RAW-REASONING")
        self.event("item/reasoning/summaryTextDelta", itemId="secret", delta="RAW-REASONING")
        self.event("item/agentMessage/delta", itemId="comment", delta="Checking the test results.")
        self.assertEqual(self.live.text(), "Checking the test results.")
        self.assertNotIn("RAW-REASONING", json.dumps(self.store.events(self.device_id, self.room["id"])))
        self.assertFalse(self.live.activity_log.entries)

    def test_completed_commentary_latest_line_is_durable_after_runtime_reload(self):
        self.event("item/agentMessage/delta", itemId="comment", delta="Checking one\nFinished the focused tests")
        self.runtime.finish(self.live, "completed")
        fresh = Runtime(self.store, None)
        snapshot = fresh.activity_snapshot(self.run["id"])
        self.assertEqual(snapshot["latest_status_line"], "Finished the focused tests")
        self.assertFalse(snapshot["activities"])

    def test_room_snapshot_flushes_throttled_output(self):
        self.event("item/started", item=command())
        with mock.patch("codex_lan.activity.time.time", return_value=2):
            self.event("item/commandExecution/outputDelta", itemId="cmd", delta="First\n")
        with mock.patch("codex_lan.activity.time.time", return_value=2.1):
            self.event("item/commandExecution/outputDelta", itemId="cmd", delta="Latest line\n")
        with mock.patch("codex_lan.activity.time.time", return_value=2.4):
            snapshot = self.runtime.snapshots(self.device_id, self.room["id"])[0]
        self.assertEqual(snapshot["latest_status_line"], "Latest line")
        self.assertEqual(snapshot["activities"][0]["output"], "First\nLatest line\n")

    def test_production_runtime_batches_stream_events_without_reordering(self):
        self.runtime.batch_events = True

        async def exercise():
            with mock.patch.object(
                self.store, "persist_runtime_batch",
                wraps=self.store.persist_runtime_batch,
            ) as persist_runtime_batch:
                for index in range(64):
                    self.runtime.emit(self.live, "delta", {"text": str(index)})
                self.assertEqual(self.store.events(self.device_id, self.room["id"]), [])
                await asyncio.sleep(0.35)
                await self.runtime.flush_events()
                self.assertEqual(persist_runtime_batch.call_count, 1)

        asyncio.run(exercise())
        events = self.store.events(self.device_id, self.room["id"])
        self.assertEqual([event["payload"]["text"] for event in events], [str(index) for index in range(64)])
        self.assertEqual(self.live.last_event_id, events[-1]["id"])

    def test_blocked_runtime_writer_does_not_block_network_event_loop(self):
        self.runtime.batch_events = True
        entered, release = threading.Event(), threading.Event()
        original = self.store.persist_runtime_batch

        def delayed(*args):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test writer was not released")
            return original(*args)

        async def exercise():
            with mock.patch.object(self.store, "persist_runtime_batch", side_effect=delayed):
                self.runtime.emit(self.live, "status", {"text": "still connected"})
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                started = time.monotonic()
                await asyncio.sleep(0.03)
                self.assertLess(time.monotonic() - started, 0.2)
                release.set()
                await asyncio.wait_for(self.runtime.flush_events(), timeout=2)

        try:
            asyncio.run(exercise())
        finally:
            release.set()
        self.assertEqual(
            self.store.events(self.device_id, self.room["id"])[0]["payload"]["text"],
            "still connected",
        )

    def test_terminal_checkpoint_preserves_more_than_stream_recovery_window(self):
        for index in range(20):
            self.event("item/completed", item=command(str(index), status="completed", aggregatedOutput=str(index)))
        self.runtime.finish(self.live, "completed")
        replay = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(len(replay["activities"]), 20)
        self.assertEqual(replay["activities"][-1]["output"], "19")

    def test_wrong_thread_and_turn_cannot_mutate_activity(self):
        self.runtime.event(self.peer, "item/started", {"threadId": "other", "turnId": "turn", "item": command()})
        self.runtime.event(self.peer, "item/started", {"threadId": "thread", "turnId": "other", "item": command()})
        self.assertFalse(self.live.activity_log.entries)

    def test_interrupted_run_marks_pending_steps_without_false_success(self):
        self.event("item/started", item=command())
        self.runtime.finish(self.live, "interrupted")
        replay = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(replay["activities"][0]["status"], "cancelled")
        self.assertEqual(replay["activity"], "Connection interrupted")


if __name__ == "__main__":
    unittest.main()
