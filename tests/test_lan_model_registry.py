"""CODEX: Provider-authoritative discovery and bounded, explicitly selected GPT-6 additions."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_lan_server as fixture
from codex_lan.model_registry import (
    MAX_MODEL_PAGES, MAX_MODELS, MODELS_URL, ModelRegistryError, model_catalog, read_model_registry,
)
from codex_lan.runtime import Peer, Runtime
from codex_lan.worker import CodexAppServer, WorkerError


class ModelRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_pages_and_preserves_hidden_exclusions_and_provider_capabilities(self):
        first = {"id": "first", "model": "gpt-6.1-sol", "isDefault": True,
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                 "serviceTiers": [{"id": "priority", "name": "Fast"}]}
        second = {"id": "second", "model": "another-provider-model"}
        request = AsyncMock(side_effect=[
            {"data": [first], "nextCursor": "page-two"},
            {"data": [{"id": "hidden", "hidden": True}, second], "nextCursor": None},
        ])
        result = await read_model_registry(request, timeout=4)
        self.assertEqual(result, [first, {"id": "hidden", "hidden": True}, second])
        self.assertIsNot(result[0], first)
        self.assertEqual(request.await_args_list[0].args,
                         ("model/list", {"limit": MAX_MODELS, "includeHidden": True}))
        self.assertEqual(request.await_args_list[1].args[1]["cursor"], "page-two")
        self.assertEqual(request.await_args_list[1].kwargs, {"timeout": 4})

    async def test_malformed_catalog_is_never_partially_accepted(self):
        invalid = (None, [], {}, {"data": None}, {"data": [None]}, {"data": [{}]},
                   {"data": [{"id": 1}]}, {"data": [{"id": ""}]},
                   {"data": [{"id": "x" * 201}]},
                   {"data": [{"id": "same"}, {"id": "same"}]})
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ModelRegistryError):
                await read_model_registry(AsyncMock(return_value=response))
        request = AsyncMock(side_effect=[{"data": [{"id": "good"}], "nextCursor": "two"},
                                         {"data": [{"id": "good"}]}])
        with self.assertRaises(ModelRegistryError):
            await read_model_registry(request)

    async def test_pagination_is_bounded_including_hidden_entries(self):
        for cursor in ("", False, {}, "x" * 4097):
            with self.subTest(cursor=cursor), self.assertRaises(ModelRegistryError):
                await read_model_registry(AsyncMock(return_value={"data": [], "nextCursor": cursor}))
        request = AsyncMock(return_value={"data": [], "nextCursor": "same"})
        with self.assertRaises(ModelRegistryError):
            await read_model_registry(request)
        self.assertEqual(request.await_count, 2)
        request = AsyncMock(side_effect=[{"data": [], "nextCursor": str(page)}
                                         for page in range(MAX_MODEL_PAGES)])
        with self.assertRaises(ModelRegistryError):
            await read_model_registry(request)
        self.assertEqual(request.await_count, MAX_MODEL_PAGES)
        hidden = [{"id": str(index), "hidden": True} for index in range(MAX_MODELS)]
        request = AsyncMock(side_effect=[{"data": hidden, "nextCursor": "two"},
                                         {"data": [{"id": "overflow"}]}])
        with self.assertRaises(ModelRegistryError):
            await read_model_registry(request)

    def test_documented_catalog_is_exact_and_marks_entitlement_unverified(self):
        models = model_catalog([])
        self.assertEqual([item["model"] for item in models],
                         ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"])
        for item in models:
            self.assertFalse(item["isDefault"])
            self.assertFalse(item["availabilityVerified"])
            self.assertEqual(item["catalogSource"], "official")
            self.assertEqual(item["catalogSourceUrl"], MODELS_URL)
            efforts = [option["reasoningEffort"] for option in item["supportedReasoningEfforts"]]
            expected = ["low", "medium", "high", "xhigh", "max"]
            if item["model"] != "gpt-6-luna":
                expected.append("ultra")
            self.assertEqual(efforts, expected)
            self.assertIn(item["defaultReasoningEffort"], efforts)
        models[0]["supportedReasoningEfforts"].clear()
        self.assertEqual(len(model_catalog([])[0]["supportedReasoningEfforts"]), 6)

    def test_provider_default_alias_efforts_and_order_take_precedence(self):
        provider = [{"id": "preferred", "model": "gpt-6.1-sol", "isDefault": True,
                     "defaultReasoningEffort": "low", "displayName": "Account Sol",
                     "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                     "serviceTiers": [{"id": "priority", "name": "Fast"}]},
                    {"id": "legacy", "model": "gpt-5.5"}]
        before = deepcopy(provider)
        models = model_catalog(provider)
        self.assertEqual([item["id"] for item in models[:2]], ["preferred", "legacy"])
        self.assertEqual(sum(item["model"] == "gpt-6.1-sol" for item in models), 1)
        self.assertEqual(models[0], {**before[0], "availabilityVerified": True,
                                    "catalogSource": "provider"})
        self.assertEqual(provider, before)

    def test_hidden_official_models_are_excluded_not_reintroduced_as_fallbacks(self):
        models = model_catalog([{"id": "hidden-sol", "model": "gpt-6.1-sol", "hidden": True},
                                {"id": "gpt-6-luna", "hidden": True}])
        self.assertNotIn("gpt-6.1-sol", [item["model"] for item in models])
        self.assertNotIn("gpt-6-luna", [item["model"] for item in models])


class RuntimeModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = Runtime(None, None)
        self.peer = Peer("device", SimpleNamespace(closed=False), ready=True,
                         models=deepcopy(fixture.MODELS))
        self.runtime.peers["device"] = self.peer

    async def test_refresh_is_single_flight_and_does_not_restart_or_reselect(self):
        advertised = {"id": "new", "model": "gpt-6.1-sol", "isDefault": False,
                      "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
        self.peer.call = AsyncMock(return_value={"data": [*self.peer.models, advertised]})
        first, second = await asyncio.gather(self.runtime.refresh_models("device"),
                                             self.runtime.refresh_models("device"))
        self.assertEqual(first, second)
        self.assertEqual(self.peer.call.await_count, 1)
        self.assertEqual(self.peer.call.await_args.args[0], "model/list")
        self.assertGreater(self.peer.model_refresh_after, time.monotonic())
        self.assertEqual(self.runtime.validate_model("device", "", ""), ("astra", "medium"))
        self.assertTrue(next(item for item in first if item["id"] == "new")["availabilityVerified"])
        with self.assertRaises(ValueError):
            self.runtime.validate_model("device", "gpt-6.1-sol", "ultra")
        await self.runtime.refresh_models("device")
        self.assertEqual(self.peer.call.await_count, 1)

    async def test_failed_refresh_preserves_previous_catalog_without_logging_provider_detail(self):
        previous = deepcopy(self.peer.models)
        for failure in (ConnectionError("secret-provider-detail"), RuntimeError("secret-provider-detail")):
            self.peer.model_refresh_after = 0
            self.peer.call = AsyncMock(side_effect=failure)
            with self.assertLogs("codex_lan.runtime", level="INFO") as messages:
                result = await self.runtime.refresh_models("device")
            self.assertEqual(self.peer.models, previous)
            self.assertEqual(result[0]["id"], "astra")
            self.assertNotIn("secret-provider-detail", str(messages.output))
            self.assertGreater(self.peer.model_refresh_after, time.monotonic())
        self.peer.model_refresh_after = 0
        self.peer.call = AsyncMock(return_value={"data": [{"id": "new"}], "nextCursor": "same"})
        await self.runtime.refresh_models("device")
        self.assertEqual(self.peer.models, previous)

    async def test_refresh_timeout_is_bounded_and_keeps_existing_models(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        self.peer.call = AsyncMock(side_effect=stalled)
        with patch("codex_lan.runtime.MODEL_REFRESH_TIMEOUT_SECONDS", 0.01):
            result = await self.runtime.refresh_models("device")
        self.assertEqual(result[0]["id"], "astra")
        self.assertEqual(self.peer.models, fixture.MODELS)

    async def test_replaced_peer_cannot_receive_previous_connections_catalog(self):
        replacement = Peer("device", SimpleNamespace(closed=False), ready=True,
                           models=[{"id": "replacement"}])
        async def replacing(*args, **kwargs):
            self.runtime.peers["device"] = replacement
            return {"data": [{"id": "stale"}]}
        self.peer.call = AsyncMock(side_effect=replacing)
        result = await self.runtime.refresh_models("device")
        self.assertEqual(result[0]["id"], "replacement")
        self.assertEqual(replacement.models, [{"id": "replacement"}])

    async def test_offline_never_advertises_manual_fallbacks(self):
        self.peer.ready = False
        self.assertEqual(await self.runtime.refresh_models("device"), [])
        self.assertEqual(self.runtime.models("device"), [])
        self.assertEqual(await self.runtime.refresh_models("missing"), [])
        with self.assertRaisesRegex(ValueError, "offline"):
            self.runtime.validate_model("device", "gpt-6.1-sol", "high")

    def test_only_bounded_documented_models_can_be_selected_explicitly(self):
        self.assertEqual(self.runtime.validate_model("device", "gpt-6.1-sol", "ultra"),
                         ("gpt-6.1-sol", "ultra"))
        self.assertEqual(self.runtime.validate_model("device", "gpt-6-sol", "max"),
                         ("gpt-6-sol", "max"))
        self.assertEqual(self.runtime.validate_model("device", "gpt-6-luna", ""),
                         ("gpt-6-luna", "medium"))
        for model, effort in (("gpt-6-luna", "ultra"), ("gpt-6.1-sol", "none"),
                              ("gpt-6-terra", "high"), ("gpt-6.1-sol-extra", "low")):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                self.runtime.validate_model("device", model, effort)
        self.peer.models = []
        self.assertEqual(self.runtime.models("device"), [])
        with self.assertRaises(ValueError):
            self.runtime.validate_model("device", "", "")
        with self.assertRaises(ValueError):
            self.runtime.validate_model("device", "gpt-6-sol", "low")


class WorkerModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_discovery_has_one_total_timeout_across_pages(self):
        pages = []
        async def request(method, params, **kwargs):
            if method == "model/list":
                pages.append(params)
                if len(pages) == 1:
                    return {"data": [{"id": "first"}], "nextCursor": "two"}
                await asyncio.Event().wait()
            return {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = CodexAppServer(root / "unused-codex", root, root, AsyncMock(), AsyncMock())
            app.request = request
            app.notify = AsyncMock()
            with patch("codex_lan.worker.MODEL_DISCOVERY_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(WorkerError, "model registry discovery timed out"):
                    await app.initialize({"accessToken": "fake-token", "chatgptAccountId": "fake-account"})
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["cursor"], "two")


class ModelApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.LanServerTests.asyncSetUp
    asyncTearDown = fixture.LanServerTests.asyncTearDown
    request = fixture.LanServerTests.request
    until = fixture.LanServerTests.until
    worker = fixture.LanServerTests.worker
    room = fixture.LanServerTests.room
    submit_run = fixture.LanServerTests.submit_run
    ready_run = fixture.LanServerTests.ready_run

    async def test_manual_family_selection_reaches_fake_worker_with_requested_effort(self):
        worker = await self.worker(self.a, "manual-gpt6")
        existing = await self.room(self.a, model="astra", effort="low")
        room = await self.room(self.a, model="gpt-6-astra", effort="max", speed="fast")
        models = (await self.request("GET", "/api/models", self.a))["models"]
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
            metadata = next(item for item in models if item["model"] == model)
            self.assertFalse(metadata["availabilityVerified"])
            room = await self.request("PATCH", f"/api/rooms/{room['id']}", self.a,
                                      json={"model": model, "effort": "max", "speed": "fast"})
            created = await self.ready_run(self.a, worker, room, request_id=model)
            params = [call["params"] for call in worker.calls if call["method"] == "turn/start"][-1]
            self.assertEqual(params["model"], model)
            self.assertEqual(params["effort"], "max")
            self.assertEqual(params["serviceTierForTurn"], "fast")
            await worker.complete()
            await self.until(lambda: created["run"]["id"] not in self.service.runtime.runs)
        saved = self.service.store.get_room(self.a["device"]["id"], existing["id"])
        self.assertEqual((saved["model"], saved["effort"]), ("astra", "low"))
        calls_before = len([call for call in worker.calls if call["method"] == "turn/start"])
        for model, effort in (("gpt-6-luna", "ultra"), ("gpt-6-terra", "high")):
            invalid = await self.room(self.a, model=model, effort=effort)
            await self.request("POST", f"/api/rooms/{invalid['id']}/runs", self.a, expected=400,
                               json={"text": "Do not send this invalid configuration", "request_id": "invalid-" + model})
        self.assertEqual(len([call for call in worker.calls if call["method"] == "turn/start"]), calls_before)


if __name__ == "__main__":
    unittest.main()
