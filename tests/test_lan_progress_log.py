"""CODEX: Preserve public progress after final answers without retaining private reasoning."""

import asyncio
import json
import time
import unittest
from unittest import mock

import test_lan_activity as fixture
from codex_lan.activity import (
    ActivityLog, MAX_PROGRESS_ENTRIES, MAX_PROGRESS_MESSAGE, MAX_PROGRESS_TEXT,
)
from codex_lan.runtime import Runtime


class ProgressArchiveTests(unittest.TestCase):
    setUp = fixture.RuntimeActivityTests.setUp
    tearDown = fixture.RuntimeActivityTests.tearDown
    event = fixture.RuntimeActivityTests.event

    def message(self, identifier, text, phase=None):
        item = {"id": identifier, "type": "agentMessage", "text": text}
        if phase is not None:
            item["phase"] = phase
        self.event("item/completed", item=item)

    def test_commentary_survives_final_reload_and_completed_delivery(self):
        self.message("first", "Inspecting the files.\nChecking the configuration.", "commentary")
        self.message("second", "The focused tests passed.", "commentary")
        self.message("answer", "The requested change is ready.", "final_answer")
        self.runtime.finish(self.live, "completed")
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual([entry["id"] for entry in snapshot["progress_log"]], ["first", "second"])
        self.assertEqual(snapshot["progress_log_count"], 2)
        self.assertEqual(snapshot["progress_log"][0]["text"], "Inspecting the files.\nChecking the configuration.")
        self.assertFalse(snapshot["activities"])
        completed = next(event for event in self.store.events(self.device_id, self.room["id"])
                         if event["kind"] == "completed")
        self.assertEqual(completed["payload"]["message"]["progress_log"], snapshot["progress_log"])
        self.assertEqual(completed["payload"]["message"]["content"], "The requested change is ready.")

    def test_missing_completed_phase_retains_started_commentary_and_final(self):
        for identifier, phase, text in (("comment", "commentary", "Checking the service."),
                                        ("answer", "final_answer", "It is healthy.")):
            self.event("item/started", item={"id": identifier, "type": "agentMessage", "phase": phase})
            self.event("item/agentMessage/delta", itemId=identifier, delta=text)
            self.message(identifier, text)
        self.runtime.finish(self.live, "completed")
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual([entry["id"] for entry in snapshot["progress_log"]], ["comment"])
        answer = next(message for message in self.store.list_messages(self.device_id, self.room["id"])
                      if message["role"] == "assistant")
        self.assertEqual(answer["content"], "It is healthy.")

    def test_legacy_messages_reserve_last_answer_but_keep_interrupted_text(self):
        self.message("first", "Inspecting.")
        self.message("second", "Testing.")
        self.message("final", "Done.")
        self.runtime.finish(self.live, "completed")
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual([entry["id"] for entry in snapshot["progress_log"]], ["first", "second"])

    def test_interrupted_legacy_last_message_is_not_lost(self):
        self.event("item/agentMessage/delta", itemId="partial", delta="Still checking the service.")
        self.runtime.finish(self.live, "interrupted")
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(snapshot["progress_log"][0]["text"], "Still checking the service.")

    def test_partial_explicit_commentary_checkpoint_survives_runtime_loss(self):
        self.event("item/started", item={"id": "comment", "type": "agentMessage", "phase": "commentary"})
        self.event("item/agentMessage/delta", itemId="comment", delta="Checking the current configuration.")
        self.runtime._flush_response_deltas(self.live, force_status=True)
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(snapshot["progress_log"][0]["text"], "Checking the current configuration.")

    def test_legacy_partial_checkpoint_survives_crash_but_success_removes_final(self):
        self.event("item/agentMessage/delta", itemId="legacy", delta="Still inspecting.")
        self.runtime._flush_response_deltas(self.live, force_status=True)
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(snapshot["progress_log"][0]["text"], "Still inspecting.")
        self.runtime.finish(self.live, "completed")
        completed = Runtime(self.store, None).activity_snapshot(self.run["id"])
        self.assertEqual(completed["progress_log"], [])
        self.assertEqual(completed["progress_log_count"], 0)

    def test_archive_redacts_credentials_and_ignores_private_protocol_items(self):
        secret = "sk-" + "x" * 24
        self.message("comment", "Observed token=" + secret + "\nBearer " + secret, "commentary")
        self.message("analysis", "PRIVATE-ANALYSIS", "analysis")
        self.event("item/completed", item={"id": "reasoning", "type": "reasoning", "text": "PRIVATE-REASONING"})
        self.event("item/reasoning/summaryTextDelta", itemId="reasoning", delta="PRIVATE-SUMMARY")
        self.message("answer", "Done.", "final_answer")
        self.runtime.finish(self.live, "completed")
        snapshot = Runtime(self.store, None).activity_snapshot(self.run["id"])
        rendered = json.dumps(snapshot["progress_log"])
        for private in (secret, "PRIVATE-ANALYSIS", "PRIVATE-REASONING", "PRIVATE-SUMMARY"):
            self.assertNotIn(private, rendered)
        self.assertIn("[redacted]", rendered)

    def test_archive_has_separate_bounded_entry_and_text_budgets(self):
        log = ActivityLog()
        for index in range(MAX_PROGRESS_ENTRIES + 20):
            log.progress(str(index), "x" * (MAX_PROGRESS_MESSAGE + 100))
        snapshot = log.progress_snapshot()
        self.assertLessEqual(len(snapshot["progress_log"]), MAX_PROGRESS_ENTRIES)
        self.assertLessEqual(sum(len(entry["text"]) for entry in snapshot["progress_log"]), MAX_PROGRESS_TEXT)
        self.assertEqual(snapshot["progress_log_count"], MAX_PROGRESS_ENTRIES + 20)
        self.assertTrue(snapshot["progress_log_truncated"])
        restored = ActivityLog()
        restored.restore_progress(snapshot["progress_log"], snapshot["progress_log_count"], True)
        self.assertEqual(restored.progress_snapshot(), snapshot)

    def test_progress_stream_is_coalesced_and_does_not_write_per_token(self):
        self.runtime.batch_events = True

        async def exercise():
            self.event("item/started", item={"id": "comment", "type": "agentMessage", "phase": "commentary"})
            started = time.monotonic()
            with mock.patch("codex_lan.runtime.time.monotonic", return_value=1000):
                for _ in range(6000):
                    self.event("item/agentMessage/delta", itemId="comment", delta="text")
                self.runtime._flush_response_deltas(self.live, force_status=True)
                self.message("comment", "text" * 6000, "commentary")
            await self.runtime.flush_events()
            self.assertLess(time.monotonic() - started, 3)

        asyncio.run(exercise())
        progress = [event for event in self.store.events(self.device_id, self.room["id"])
                    if event["kind"] == "progress"]
        self.assertLessEqual(len(progress), 3)
        self.assertTrue(self.live.activity_log.progress_snapshot()["progress_log_truncated"])


if __name__ == "__main__":
    unittest.main()
