"""CODEX: Worker approval boundaries for read-only and full-access turns."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_lan.worker import WorkerConnection


class WorkerReadOnlyApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.worker = object.__new__(WorkerConnection)
        self.worker.codex = SimpleNamespace(
            respond=mock.AsyncMock(),
            respond_error=mock.AsyncMock(),
        )
        self.worker.channel = SimpleNamespace(send=mock.AsyncMock())
        self.worker.turn_requests = {
            "read-modern": {
                "threadId": "read-modern",
                "sandboxPolicy": {"type": "readOnly"},
            },
            "read-legacy": {
                "threadId": "read-legacy",
                "sandboxPolicy": {"type": "readOnly"},
            },
            "full-modern": {
                "threadId": "full-modern",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
            "full-legacy": {
                "threadId": "full-legacy",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        }

    def reset_mocks(self) -> None:
        self.worker.codex.respond.reset_mock()
        self.worker.codex.respond_error.reset_mock()
        self.worker.channel.send.reset_mock()

    async def assert_direct_response(
        self, method: str, params: dict[str, object], expected: dict[str, object]
    ) -> None:
        self.reset_mocks()
        await self.worker._handle_app_request(41, method, params)
        self.worker.codex.respond.assert_awaited_once_with(41, expected)
        self.worker.codex.respond_error.assert_not_awaited()
        self.worker.channel.send.assert_not_awaited()

    async def assert_legacy_denial(
        self, method: str, params: dict[str, object]
    ) -> None:
        self.reset_mocks()
        await self.worker._handle_app_request(42, method, params)
        self.worker.codex.respond.assert_awaited_once()
        identifier, payload = self.worker.codex.respond.await_args.args
        self.assertEqual(identifier, 42)
        self.assertIsInstance(payload, dict)
        decision = payload.get("decision")
        if decision == "abort":
            self.worker.codex.respond_error.assert_not_awaited()
            self.worker.channel.send.assert_not_awaited()
            return
        self.assertIsInstance(decision, dict)
        self.assertEqual(set(decision), {"denied"})
        denied = decision["denied"]
        self.assertIsInstance(denied, dict)
        self.assertIsInstance(denied.get("rejection"), str)
        self.assertTrue(denied["rejection"].strip())
        self.worker.codex.respond_error.assert_not_awaited()
        self.worker.channel.send.assert_not_awaited()

    def test_read_only_lookup_supports_modern_and_legacy_thread_identifiers(self) -> None:
        self.assertTrue(self.worker._request_is_read_only({"threadId": "read-modern"}))
        self.assertTrue(
            self.worker._request_is_read_only({"conversationId": "read-legacy"})
        )
        self.assertFalse(self.worker._request_is_read_only({"threadId": "full-modern"}))
        self.assertFalse(
            self.worker._request_is_read_only({"conversationId": "full-legacy"})
        )

    def test_unknown_missing_and_malformed_turns_fail_closed(self) -> None:
        self.worker.turn_requests["missing-policy"] = {"threadId": "missing-policy"}
        self.worker.turn_requests["malformed-policy"] = {
            "threadId": "malformed-policy",
            "sandboxPolicy": "dangerFullAccess",
        }
        for params in (
            {},
            {"threadId": "unknown"},
            {"conversationId": "unknown"},
            {"threadId": "missing-policy"},
            {"threadId": "malformed-policy"},
        ):
            with self.subTest(params=params):
                self.assertTrue(self.worker._request_is_read_only(params))

    async def test_read_only_denies_modern_command_and_file_approvals(self) -> None:
        for method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            with self.subTest(method=method):
                await self.assert_direct_response(
                    method,
                    {"threadId": "read-modern"},
                    {"decision": "decline"},
                )

    async def test_read_only_denies_legacy_command_and_file_approvals(self) -> None:
        for method in ("execCommandApproval", "applyPatchApproval"):
            with self.subTest(method=method):
                await self.assert_legacy_denial(
                    method, {"conversationId": "read-legacy"}
                )

    async def test_read_only_permission_request_grants_nothing(self) -> None:
        await self.assert_direct_response(
            "item/permissions/requestApproval",
            {
                "threadId": "read-modern",
                "permissions": {
                    "fileSystem": {"write": ["/work/project"]},
                    "network": {"enabled": True},
                },
            },
            {"permissions": {}},
        )

    async def test_unknown_or_missing_turn_never_approves_privileged_requests(self) -> None:
        cases = (
            ("item/commandExecution/requestApproval", {}, {"decision": "decline"}),
            ("item/fileChange/requestApproval", {"threadId": "unknown"}, {"decision": "decline"}),
            ("item/permissions/requestApproval", {}, {"permissions": {}}),
        )
        for method, params, expected in cases:
            with self.subTest(method=method, params=params):
                await self.assert_direct_response(method, params, expected)

    async def test_full_access_keeps_existing_command_and_file_approvals(self) -> None:
        cases = (
            (
                "item/commandExecution/requestApproval",
                {"threadId": "full-modern"},
                {"decision": "accept"},
            ),
            (
                "item/fileChange/requestApproval",
                {"threadId": "full-modern"},
                {"decision": "accept"},
            ),
            (
                "execCommandApproval",
                {"conversationId": "full-legacy"},
                {"decision": "approved"},
            ),
            (
                "applyPatchApproval",
                {"conversationId": "full-legacy"},
                {"decision": "approved"},
            ),
        )
        for method, params, expected in cases:
            with self.subTest(method=method):
                await self.assert_direct_response(method, params, expected)


if __name__ == "__main__":
    unittest.main()
