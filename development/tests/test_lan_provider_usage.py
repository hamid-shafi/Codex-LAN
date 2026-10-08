"""CODEX: Provider quota normalization and cached read-only probes without real account calls."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_layout import configure_imports, source_file
configure_imports(__file__)

from codex_lan.auth import AuthProvider
from codex_lan.provider_usage import ProviderUsage, normalize_rate_limits


def window(percent=25, minutes=300, reset=1789012345):
    return {"usedPercent": percent, "windowDurationMins": minutes, "resetsAt": reset}


def response(percent=25):
    return {"rateLimits": {"limitId": "codex", "limitName": "Codex", "primary": window(percent),
                           "secondary": window(12.5, 10080, 1789500000)}}


class NormalizationTests(unittest.TestCase):
    def test_primary_and_weekly_windows_are_provider_values_not_token_conversions(self):
        payload = response()
        payload.update(tokens=987654321, accountId="PRIVATE-ACCOUNT", credits={"balance": "PRIVATE-CREDIT"})
        payload["rateLimitResetCredits"] = {"credits": [{"id": "PRIVATE-RESET-ID"}]}
        normalized = normalize_rate_limits(payload, fetched_at=100)
        self.assertTrue(normalized["available"])
        self.assertFalse(normalized["stale"])
        self.assertEqual(normalized["fetched_at"], 100)
        self.assertEqual([item["used_percent"] for item in normalized["windows"]], [25, 12.5])
        self.assertEqual([item["window_minutes"] for item in normalized["windows"]], [300, 10080])
        self.assertNotIn("PRIVATE", json.dumps(normalized))
        self.assertNotIn("tokens", json.dumps(normalized))
        self.assertEqual(set(normalized["windows"][0]), {"limit_id", "limit_name", "kind", "used_percent", "window_minutes", "resets_at"})

    def test_multiple_buckets_override_duplicate_legacy_snapshot(self):
        payload = response(10)
        payload["rateLimitsByLimitId"] = {"codex": {"limitId": "codex", "primary": window(20)},
                                           "codex_other": {"limitName": "Other quota", "primary": window(40)}}
        result = normalize_rate_limits(payload)
        self.assertEqual([(item["limit_id"], item["used_percent"]) for item in result["windows"]], [("codex", 20), ("codex_other", 40)])

    def test_missing_or_invalid_percent_never_becomes_zero(self):
        for payload in (None, [], {}, {"rateLimits": {}}, {"rateLimitsByLimitId": {}},
                        {"rateLimits": {"primary": {"windowDurationMins": 300}}}):
            with self.subTest(payload=payload):
                result = normalize_rate_limits(payload)
                self.assertFalse(result["available"])
                self.assertEqual(result["windows"], [])
        for value in (None, "25", True, -1, 101, float("nan"), float("inf"), 10 ** 500):
            result = normalize_rate_limits({"rateLimits": {"primary": window(value)}})
            self.assertFalse(result["available"])
        self.assertTrue(normalize_rate_limits({"rateLimits": {"primary": window(0)}})["available"])

    def test_unknown_window_metadata_stays_unknown(self):
        result = normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": 25}}})
        self.assertIsNone(result["windows"][0]["window_minutes"])
        self.assertIsNone(result["windows"][0]["resets_at"])
        invalid = normalize_rate_limits({"rateLimits": {"primary": window(10, -10, float("nan"))}})
        self.assertIsNone(invalid["windows"][0]["window_minutes"])
        self.assertIsNone(invalid["windows"][0]["resets_at"])

    def test_sensitive_labels_and_bucket_shapes_are_filtered_and_bounded(self):
        payload = {"rateLimitsByLimitId": {"codex": {"limitName": "Bearer SECRET-TOKEN", "primary": window(10)},
                                          "account_secret": {"primary": window(20)},
                                          "raw@example.com": {"primary": window(30)}}}
        result = normalize_rate_limits(payload)
        self.assertEqual(len(result["windows"]), 1)
        self.assertIsNone(result["windows"][0]["limit_name"])
        self.assertNotIn("SECRET", json.dumps(result))
        payload = {"rateLimitsByLimitId": {f"quota-{index}": {"primary": window(1), "secondary": window(2)} for index in range(1000)}}
        self.assertLessEqual(len(normalize_rate_limits(payload)["windows"]), 32)


class FakeProcess:
    def __init__(self, result=None, error=None, hang=False, stubborn=False):
        self.stdin = mock.Mock()
        self.stdin.drain = mock.AsyncMock()
        packets = [{"id": 1, "result": {}}, {"method": "account/updated", "params": {"accountId": "PRIVATE-ACCOUNT"}}]
        if not hang:
            packets.append({"id": 2, "error": {"message": error}} if error else {"id": 2, "result": result if result is not None else response()})
        self.lines = [(json.dumps(packet) + "\n").encode() for packet in packets]
        self.hang, self.stubborn = hang, stubborn
        self.stdout = mock.Mock()
        self.stdout.readline = self.readline
        self.returncode = None
        self.terminated = self.killed = False

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        return b""

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode


class FakeGateway:
    def __init__(self, **options):
        self.options = options
        self.start = mock.AsyncMock()
        self.wait_ready = mock.AsyncMock()
        self.report_failure = mock.AsyncMock()
        self.report_success = mock.AsyncMock()
        self.close = mock.AsyncMock()

    def environment(self, env):
        return dict(env, HTTPS_PROXY="http://127.0.0.1:19876")


class ProviderReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway = mock.patch("codex_lan.provider_usage.AdaptiveProxy", FakeGateway)
        gateway.start()
        self.addCleanup(gateway.stop)

    async def test_auth_method_only_runs_readonly_quota_handshake_and_cleans_up(self):
        process = FakeProcess()
        provider = AuthProvider("/fake/host-auth", codex="/fake/codex")
        with mock.patch("codex_lan.provider_usage.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)) as spawn:
            with mock.patch.object(provider, "_read", side_effect=AssertionError("Credential read is not needed")), mock.patch.object(provider, "_refresh", side_effect=AssertionError("No forced refresh")):
                result = await provider.rate_limits()
        packets = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([packet["method"] for packet in packets], ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(spawn.call_args.args, ("/fake/codex", "app-server", "--stdio"))
        self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_HOME"], "/fake/host-auth")
        self.assertEqual(spawn.call_args.kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:19876")
        provider.provider_usage.gateway.start.assert_awaited_once()
        provider.provider_usage.gateway.wait_ready.assert_not_awaited()
        provider.provider_usage.gateway.report_success.assert_awaited_once_with(scope="usage")
        self.assertTrue(process.terminated)
        self.assertTrue(result["available"])
        self.assertNotIn("PRIVATE-ACCOUNT", json.dumps(result))

    async def test_concurrent_reads_share_one_probe_and_return_independent_copies(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch():
            entered.set()
            await release.wait()
            return response()

        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(side_effect=fetch)) as query:
            tasks = [asyncio.create_task(reader.read()) for _index in range(8)]
            await entered.wait()
            release.set()
            values = await asyncio.gather(*tasks)
            values[0]["windows"][0]["used_percent"] = 99
            next_value = await reader.read()
            self.assertEqual(next_value["windows"][0]["used_percent"], 25)
            query.assert_awaited_once()

    async def test_last_success_is_explicitly_stale_on_failure_and_error_is_sanitized(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(return_value=response())):
            good = await reader.read()
        reader.expires_at = 0
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(side_effect=RuntimeError("Bearer SECRET-TOKEN account=PRIVATE"))):
            stale = await reader.read()
        self.assertTrue(stale["available"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["windows"], good["windows"])
        self.assertEqual(stale["fetched_at"], good["fetched_at"])
        self.assertNotIn("SECRET", json.dumps(stale))
        self.assertNotIn("PRIVATE", json.dumps(stale))

    async def test_missing_supported_data_clears_old_snapshot_instead_of_claiming_zero(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(return_value=response())):
            await reader.read()
        reader.expires_at = 0
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(return_value={})):
            missing = await reader.read()
        self.assertFalse(missing["available"])
        self.assertEqual(missing["windows"], [])
        self.assertIsNone(reader.last_good)

    async def test_failed_probe_is_cached_and_returns_no_private_error(self):
        process = FakeProcess(error="PRIVATE-ACCOUNT SECRET-TOKEN")
        reader = ProviderUsage("/fake", "/fake/codex")
        with mock.patch("codex_lan.provider_usage.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)) as spawn:
            first = await reader.read()
            second = await reader.read()
        self.assertEqual(first, second)
        self.assertFalse(first["available"])
        self.assertIsNone(first["fetched_at"])
        self.assertNotIn("SECRET", json.dumps(first))
        spawn.assert_awaited_once()
        self.assertTrue(process.terminated)

    async def test_timeout_kills_stubborn_probe_with_bounded_cleanup(self):
        process = FakeProcess(hang=True, stubborn=True)
        reader = ProviderUsage("/fake", "/fake/codex")
        with mock.patch("codex_lan.provider_usage.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)), mock.patch("codex_lan.provider_usage.REQUEST_TIMEOUT", 0.01), mock.patch("codex_lan.provider_usage.PROCESS_STOP_TIMEOUT", 0.01):
            result = await asyncio.wait_for(reader.read(), timeout=0.2)
        self.assertFalse(result["available"])
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    async def test_request_cancellation_reaps_probe_and_does_not_cache_failure(self):
        process = FakeProcess(hang=True)
        reader = ProviderUsage("/fake", "/fake/codex")
        with mock.patch("codex_lan.provider_usage.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)):
            task = asyncio.create_task(reader.read())
            while not process.stdin.write.called:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)
        self.assertIsNone(reader.cached)

    async def test_quota_read_does_not_wait_for_worker_auth_refresh_lock(self):
        provider = AuthProvider("/fake", codex="/fake/codex")
        await provider.lock.acquire()
        try:
            with mock.patch.object(provider.provider_usage, "_fetch", new=mock.AsyncMock(return_value=response())):
                result = await asyncio.wait_for(provider.rate_limits(), timeout=0.2)
            self.assertTrue(result["available"])
        finally:
            provider.lock.release()

    async def test_reset_crossing_refreshes_and_never_fabricates_new_allowance(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        value = {"rateLimits": {"primary": window(91, 300, 1005)}}
        clock = {"wall": 1000, "mono": 10}
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(return_value=value)) as fetch, mock.patch("codex_lan.provider_usage.time.time", side_effect=lambda: clock["wall"]), mock.patch("codex_lan.provider_usage.time.monotonic", side_effect=lambda: clock["mono"]):
            first = await reader.read()
            self.assertFalse(first["stale"])
            self.assertLessEqual(reader.expires_at, 15)
            clock.update(wall=1006, mono=11)
            expired = await reader.read()
            self.assertTrue(expired["stale"])
            self.assertEqual(expired["windows"][0]["used_percent"], 91)
            self.assertIn("reset time has passed", expired["error"])
            await reader.read()
            self.assertEqual(fetch.await_count, 2)

    async def test_expired_short_window_does_not_invalidate_fresh_weekly_window(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        value = {"rateLimits": {"primary": window(91, 300, 995), "secondary": window(17, 10080, 2000)}}
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(return_value=value)) as fetch, mock.patch("codex_lan.provider_usage.time.time", return_value=1000):
            first = await reader.read()
            second = await reader.read()
        self.assertTrue(first["available"])
        self.assertFalse(first["stale"])
        self.assertTrue(first["windows"][0]["stale"])
        self.assertFalse(first["windows"][1].get("stale", False))
        self.assertEqual(first["windows"][1]["used_percent"], 17)
        self.assertEqual(first, second)
        fetch.assert_awaited_once()

    async def test_manual_refresh_bypasses_normal_cache_and_coalesces_repeated_clicks(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        clock = {"mono": 10}
        isolated_clock = mock.Mock()
        isolated_clock.monotonic.side_effect = lambda: clock["mono"]
        isolated_clock.time.return_value = 1788950000
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(side_effect=[response(20), response(21)])) as fetch, mock.patch("codex_lan.provider_usage.time", isolated_clock):
            self.assertEqual((await reader.read())["windows"][0]["used_percent"], 20)
            clock["mono"] = 20
            self.assertEqual((await reader.read())["windows"][0]["used_percent"], 20)
            refreshed = await asyncio.gather(*(reader.read(force=True) for _index in range(6)))
            self.assertTrue(all(value["windows"][0]["used_percent"] == 21 for value in refreshed))
            self.assertEqual(fetch.await_count, 2)

    async def test_transient_failure_retries_before_normal_cache_expiry(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        clock = {"mono": 10}
        isolated_clock = mock.Mock()
        isolated_clock.monotonic.side_effect = lambda: clock["mono"]
        isolated_clock.time.return_value = 1788950000
        with mock.patch.object(reader, "_fetch", new=mock.AsyncMock(side_effect=[TimeoutError(), response(21)])) as fetch, mock.patch("codex_lan.provider_usage.time", isolated_clock):
            self.assertFalse((await reader.read())["available"])
            clock["mono"] = 15
            self.assertFalse((await reader.read())["available"])
            clock["mono"] = 21
            self.assertTrue((await reader.read())["available"])
            self.assertEqual(fetch.await_count, 2)

    async def test_unreachable_route_is_bounded_and_next_read_reuses_route_state(self):
        reader = ProviderUsage("/fake", "/fake/codex")
        gateway = reader.gateway
        gateway.start.side_effect = [asyncio.TimeoutError(), None]
        with mock.patch("codex_lan.provider_usage.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=FakeProcess())) as spawn:
            failed = await reader.read()
            self.assertFalse(failed["available"])
            spawn.assert_not_awaited()
            gateway.close.assert_not_awaited()
            reader.expires_at = 0
            recovered = await reader.read()
            self.assertTrue(recovered["available"])
            self.assertIs(reader.gateway, gateway)
            self.assertEqual(gateway.start.await_count, 2)
        await reader.close()
        gateway.close.assert_awaited_once()

    async def test_auth_refresh_uses_same_gateway_and_owner_closes_it(self):
        provider = AuthProvider("/fake/host-auth", codex="/fake/codex")
        process = FakeProcess(result={})
        with mock.patch("codex_lan.auth.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)) as spawn:
            await provider._refresh()
        provider.provider_usage.gateway.start.assert_awaited_once()
        provider.provider_usage.gateway.wait_ready.assert_not_awaited()
        provider.provider_usage.gateway.report_success.assert_awaited_once_with(scope="authentication")
        self.assertEqual(spawn.call_args.kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:19876")
        self.assertTrue(process.terminated)
        await provider.close()
        provider.provider_usage.gateway.close.assert_awaited_once()

    async def test_auth_refresh_transport_errors_after_spawn_report_route_failure(self):
        for error in (OSError("offline"), ConnectionResetError("reset"), BrokenPipeError("closed")):
            with self.subTest(error=type(error).__name__):
                provider = AuthProvider("/fake/host-auth", codex="/fake/codex")
                process = FakeProcess(result={})
                process.stdin.drain.side_effect = error
                spawn = mock.AsyncMock(return_value=process)
                with mock.patch("codex_lan.auth.asyncio.create_subprocess_exec", new=spawn):
                    with self.assertRaisesRegex(RuntimeError, "authentication service disconnected"):
                        await provider._refresh()
                spawn.assert_awaited_once()
                provider.provider_usage.gateway.report_failure.assert_awaited_once_with(scope="authentication")
                provider.provider_usage.gateway.report_success.assert_not_awaited()
                self.assertTrue(process.terminated)

    async def test_auth_refresh_rpc_error_does_not_penalize_connection_route(self):
        provider = AuthProvider("/fake/host-auth", codex="/fake/codex")
        process = FakeProcess(error="invalid host sign-in")
        with mock.patch("codex_lan.auth.asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=process)):
            with self.assertRaisesRegex(RuntimeError, "sign in on the host again"):
                await provider._refresh()
        provider.provider_usage.gateway.report_failure.assert_not_awaited()
        provider.provider_usage.gateway.report_success.assert_not_awaited()
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
