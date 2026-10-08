"""CODEX: Exercise adaptive routing with local fake streams; no provider requests or paid inference."""

import asyncio
import contextlib
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from codex_lan.egress import AdaptiveProxy, EgressError, RoutePreferenceStore


class AdaptiveProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.proxies = []
        self.streams = []
        self.background = set()

    async def asyncTearDown(self):
        for proxy in self.proxies:
            await proxy.close()
        for stream in self.streams:
            stream.close()
            with contextlib.suppress(Exception):
                await stream.wait_closed()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for task in tuple(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)

    async def listen(self, handler):
        def accept(reader, writer):
            task = asyncio.create_task(handler(reader, writer))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def proxy(self, **kwargs):
        kwargs.setdefault("proxy_url", "socks5h://proxy.test:8124")
        proxy = AdaptiveProxy(retry_delay=0, connect_timeout=0.2, **kwargs)
        self.proxies.append(proxy)
        await proxy.start()
        return proxy

    async def connect(self, proxy, target="chatgpt.com:443"):
        reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
        self.streams.append(writer)
        writer.write(("CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n\r\n").encode())
        await writer.drain()
        return reader, writer

    async def echo(self, reader, writer):
        try:
            while chunk := await reader.read(4096):
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_environment_is_explicit_and_preserves_lan_bypass_without_mutating_input(self):
        proxy = await self.proxy()
        original = {"HTTPS_PROXY": "http://old", "all_proxy": "socks5://old", "NO_PROXY": "*,chatgpt.com",
                    "OPENAI_API_KEY": "fixture", "LANG": "en_US.UTF-8"}
        result = proxy.environment(original)
        self.assertEqual(original["HTTPS_PROXY"], "http://old")
        self.assertEqual(result["HTTPS_PROXY"], proxy.url)
        self.assertEqual(result["http_proxy"], proxy.url)
        self.assertEqual(result["ALL_PROXY"], proxy.url)
        self.assertIn("192.168.0.0/16", result["NO_PROXY"])
        self.assertNotIn("*", result["NO_PROXY"])
        self.assertNotIn("chatgpt.com", result["NO_PROXY"])
        self.assertEqual(result["OPENAI_API_KEY"], "fixture")

    async def test_default_without_private_proxy_configuration_never_switches_to_proxy(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            proxy = AdaptiveProxy(retry_delay=0)
        self.proxies.append(proxy)
        await proxy.start()
        for _index in range(5):
            await proxy.report_failure(immediate=True)
        self.assertEqual(proxy.snapshot()["mode"], "direct")
        self.assertEqual(proxy.snapshot()["switches"], 0)
        self.assertEqual(proxy.snapshot()["state"], "reconnecting")

    async def test_private_proxy_environment_keeps_existing_route_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoutePreferenceStore(directory + "/egress.sqlite3")
            store.record_success("proxy")
            with mock.patch.dict("os.environ", {"CODEX_LAN_PROXY_URL": "socks5h://proxy.test:8124"}):
                configured = AdaptiveProxy(route_store=store)
            self.proxies.append(configured)
            await configured.start()
            self.assertEqual(configured.snapshot()["mode"], "proxy")
            with mock.patch.dict("os.environ", {}, clear=True):
                direct = AdaptiveProxy(route_store=store)
            self.proxies.append(direct)
            await direct.start()
            self.assertEqual(direct.snapshot()["mode"], "direct")
            self.assertEqual(store.read()["preferred_mode"], "proxy")

    def test_explicit_direct_configuration_overrides_proxy_environment(self):
        with mock.patch.dict("os.environ", {"CODEX_LAN_PROXY_URL": "socks5h://proxy.test:8124"}):
            direct = AdaptiveProxy(proxy_url="")
        self.assertEqual(direct.snapshot()["mode"], "direct")
        self.assertFalse(direct._proxy_configured)

    async def test_two_provider_failures_switch_route_and_explicit_success_resets_attempt(self):
        events = []
        proxy = await self.proxy(on_status=events.append)
        value = await proxy.report_failure(scope="thread-fixture", message="secret provider error")
        self.assertEqual((value["attempt"], value["mode"]), (1, "direct"))
        value = await proxy.report_failure(scope="thread-fixture")
        self.assertEqual((value["mode"], value["state"], value["switches"]), ("proxy", "switching", 1))
        value = await proxy.report_success()
        self.assertEqual((value["state"], value["attempt"]), ("connected", 0))
        self.assertEqual(events[0]["scope"], "thread-fixture")
        self.assertNotIn("secret", repr(events))

    async def test_direct_tunnel_survives_mode_changes_and_does_not_mark_provider_connected(self):
        port = await self.listen(self.echo)
        opened = []

        async def open_connection(host, target_port):
            opened.append((host, target_port))
            return await asyncio.open_connection("127.0.0.1", port)

        proxy = await self.proxy(open_connection=open_connection)
        reader, writer = await self.connect(proxy)
        self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
        self.assertEqual(proxy.snapshot()["state"], "connecting")
        writer.write(b"opaque TLS bytes")
        await writer.drain()
        self.assertEqual(await reader.readexactly(16), b"opaque TLS bytes")
        for _index in range(2):
            await proxy.report_failure()
        writer.write(b"still open")
        await writer.drain()
        self.assertEqual(await reader.readexactly(10), b"still open")
        self.assertEqual(opened, [("chatgpt.com", 443)])

    async def test_failed_saved_route_immediately_uses_socks_and_persists_it(self):
        requests = []

        async def socks(reader, writer):
            try:
                self.assertEqual(await reader.readexactly(3), b"\x05\x01\x00")
                writer.write(b"\x05\x00")
                await writer.drain()
                self.assertEqual(await reader.readexactly(4), b"\x05\x01\x00\x03")
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
                port = int.from_bytes(await reader.readexactly(2), "big")
                requests.append((host, port))
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
                await writer.drain()
                await self.echo(reader, writer)
            finally:
                writer.close()

        socks_port = await self.listen(socks)
        attempts = []

        async def open_connection(host, port):
            attempts.append((host, port))
            if host == "chatgpt.com":
                raise OSError("fixture unavailable")
            return await asyncio.open_connection("127.0.0.1", socks_port)

        with tempfile.TemporaryDirectory() as directory:
            state = directory + "/egress.sqlite3"
            proxy = await self.proxy(open_connection=open_connection, proxy_url="socks5h://proxy.test:8124",
                                     route_state_path=state)
            reader, writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            writer.write(b"one request")
            await writer.drain()
            self.assertEqual(await reader.readexactly(11), b"one request")
            self.assertEqual(attempts, [("chatgpt.com", 443), ("proxy.test", 8124)])
            self.assertEqual(requests, [("chatgpt.com", 443)])
            self.assertEqual(proxy.snapshot()["switches"], 1)
            restarted = await self.proxy(open_connection=open_connection, proxy_url="socks5h://proxy.test:8124",
                                         route_state_path=state)
            self.assertEqual((restarted.snapshot()["mode"], restarted.snapshot()["preferred_mode"]),
                             ("proxy", "proxy"))

    async def test_connect_ack_alone_does_not_replace_the_saved_route(self):
        async def socks(reader, writer):
            try:
                self.assertEqual(await reader.readexactly(3), b"\x05\x01\x00")
                writer.write(b"\x05\x00")
                await writer.drain()
                self.assertEqual(await reader.readexactly(4), b"\x05\x01\x00\x03")
                size = (await reader.readexactly(1))[0]
                await reader.readexactly(size + 2)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
                await writer.drain()
                await reader.read()
            finally:
                writer.close()

        socks_port = await self.listen(socks)

        async def open_connection(host, port):
            if host == "chatgpt.com":
                raise OSError("fixture unavailable")
            return await asyncio.open_connection("127.0.0.1", socks_port)

        with tempfile.TemporaryDirectory() as directory:
            state = directory + "/egress.sqlite3"
            proxy = await self.proxy(open_connection=open_connection, proxy_url="socks5h://proxy.test:8124",
                                     route_state_path=state)
            reader, _writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            self.assertEqual(RoutePreferenceStore(state).read()["preferred_mode"], "direct")
            restarted = await self.proxy(open_connection=open_connection, proxy_url="socks5h://proxy.test:8124",
                                         route_state_path=state)
            self.assertEqual(restarted.snapshot()["mode"], "direct")

    async def test_socks_authentication_is_sent_to_proxy_and_not_exposed_in_status(self):
        seen = []

        async def socks(reader, writer):
            try:
                self.assertEqual(await reader.readexactly(4), b"\x05\x02\x00\x02")
                writer.write(b"\x05\x02")
                await writer.drain()
                version, length = await reader.readexactly(2)
                user = await reader.readexactly(length)
                size = (await reader.readexactly(1))[0]
                password = await reader.readexactly(size)
                seen.append((version, user, password))
                writer.write(b"\x01\x01")
                await writer.drain()
            finally:
                writer.close()

        port = await self.listen(socks)
        proxy = await self.proxy(proxy_url=f"socks5h://fixture-user:fixture-pass@127.0.0.1:{port}")
        for _index in range(2):
            await proxy.report_failure()
        with self.assertRaises(EgressError) as raised:
            await proxy._open_target("chatgpt.com", 443)
        self.assertEqual(seen, [(1, b"fixture-user", b"fixture-pass")])
        self.assertNotIn("fixture-pass", str(raised.exception) + repr(proxy.snapshot()))

    async def test_private_lan_stays_direct_when_external_route_is_proxy(self):
        port = await self.listen(self.echo)
        opened = []

        async def open_connection(host, target_port):
            opened.append((host, target_port))
            return await asyncio.open_connection("127.0.0.1", port)

        proxy = await self.proxy(open_connection=open_connection)
        for _index in range(2):
            await proxy.report_failure()
        reader, _writer = await self.connect(proxy, "192.168.1.56:443")
        self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
        self.assertEqual(opened, [("192.168.1.56", 443)])

    async def test_closed_client_cancels_pending_dial_without_leaving_a_retry_task(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def never_connect(_host, _port):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        proxy = await self.proxy(open_connection=never_connect)
        _reader, writer = await self.connect(proxy)
        await started.wait()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
        for _index in range(20):
            if not proxy._tasks:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(proxy._tasks)

    async def test_remote_eof_before_first_upstream_byte_counts_as_route_failure(self):
        async def close_without_data(_reader, writer):
            writer.close()
            await writer.wait_closed()

        port = await self.listen(close_without_data)

        async def open_connection(_host, _target_port):
            return await asyncio.open_connection("127.0.0.1", port)

        proxy = await self.proxy(open_connection=open_connection)
        original = proxy.report_failure
        with mock.patch.object(proxy, "report_failure", new=mock.AsyncMock(wraps=original)) as failure:
            reader, _writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            await asyncio.wait_for(reader.read(), 1)
            for _index in range(20):
                if failure.await_count:
                    break
                await asyncio.sleep(0.01)
        failure.assert_awaited_once_with(route="direct")
        self.assertEqual((proxy.snapshot()["mode"], proxy.snapshot()["attempt"]), ("direct", 1))

    async def test_remote_reset_before_first_upstream_byte_counts_as_route_failure(self):
        class ResetReader:
            async def read(self, _size):
                raise ConnectionResetError("fixture reset")

        class RemoteWriter:
            def __init__(self):
                self.closed = False

            def write(self, _data):
                return None

            async def drain(self):
                return None

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        remote = RemoteWriter()

        async def open_connection(_host, _target_port):
            return ResetReader(), remote

        proxy = await self.proxy(open_connection=open_connection)
        original = proxy.report_failure
        with mock.patch.object(proxy, "report_failure", new=mock.AsyncMock(wraps=original)) as failure:
            reader, _writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            await asyncio.wait_for(reader.read(), 1)
            for _index in range(20):
                if failure.await_count:
                    break
                await asyncio.sleep(0.01)
        failure.assert_awaited_once_with(route="direct")
        self.assertTrue(remote.closed)

    async def test_remote_eof_after_upstream_data_does_not_penalize_route(self):
        async def send_then_close(_reader, writer):
            writer.write(b"upstream TLS byte")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        port = await self.listen(send_then_close)

        async def open_connection(_host, _target_port):
            return await asyncio.open_connection("127.0.0.1", port)

        proxy = await self.proxy(open_connection=open_connection)
        original = proxy.report_failure
        with mock.patch.object(proxy, "report_failure", new=mock.AsyncMock(wraps=original)) as failure:
            reader, _writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            self.assertEqual(await asyncio.wait_for(reader.readexactly(17), 1), b"upstream TLS byte")
            await asyncio.wait_for(reader.read(), 1)
            for _index in range(20):
                if not proxy._tasks:
                    break
                await asyncio.sleep(0.01)
        failure.assert_not_awaited()
        self.assertEqual(proxy.snapshot()["attempt"], 0)

    async def test_client_eof_after_connect_does_not_penalize_route(self):
        release = asyncio.Event()

        async def hold_remote(_reader, writer):
            try:
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        port = await self.listen(hold_remote)

        async def open_connection(_host, _target_port):
            return await asyncio.open_connection("127.0.0.1", port)

        proxy = await self.proxy(open_connection=open_connection)
        original = proxy.report_failure
        with mock.patch.object(proxy, "report_failure", new=mock.AsyncMock(wraps=original)) as failure:
            reader, writer = await self.connect(proxy)
            self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1))
            writer.close()
            await writer.wait_closed()
            for _index in range(20):
                if not proxy._tasks:
                    break
                await asyncio.sleep(0.01)
        release.set()
        failure.assert_not_awaited()
        self.assertEqual(proxy.snapshot()["attempt"], 0)

    async def test_only_bounded_https_connect_requests_are_accepted(self):
        opened = mock.AsyncMock(side_effect=AssertionError("Must not dial"))
        proxy = await self.proxy(open_connection=opened)
        for target in ("chatgpt.com:80", "user:secret@chatgpt.com:443", "chatgpt.com:443/path"):
            reader, _writer = await self.connect(proxy, target)
            self.assertIn(b"400", await asyncio.wait_for(reader.read(), 1))
        reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
        self.streams.append(writer)
        writer.write(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nX: " + b"a" * 17000 + b"\r\n\r\n")
        await writer.drain()
        self.assertIn(b"400", await asyncio.wait_for(reader.read(), 1))
        opened.assert_not_called()

    async def test_wait_ready_continues_across_both_routes_and_is_cancelable(self):
        proxy = await self.proxy()
        with mock.patch.object(proxy, "_probe", new=mock.AsyncMock(side_effect=[OSError("offline")] * 4 + [None])) as probe:
            result = await asyncio.wait_for(proxy.wait_ready(scope="readiness"), 1)
        self.assertEqual(probe.await_count, 5)
        self.assertEqual((result["state"], result["switches"]), ("connected", 2))
        entered = asyncio.Event()

        async def blocked_probe():
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(proxy, "_probe", new=blocked_probe):
            task = asyncio.create_task(proxy.wait_ready())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    def test_bad_proxy_configuration_never_includes_credentials_in_errors(self):
        for value in ("http://fixture-user:fixture-pass@proxy:8124", "socks5://proxy/no", "socks5://proxy:99999"):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                AdaptiveProxy(value)
            self.assertNotIn("fixture-pass", str(raised.exception))

    def test_route_database_survives_restart_and_keeps_only_non_secret_health(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/egress.sqlite3"
            first = RoutePreferenceStore(path)
            self.assertEqual(first.read()["preferred_mode"], "direct")
            saved = first.record_success("proxy")
            self.assertEqual(saved["preferred_mode"], "proxy")
            reopened = RoutePreferenceStore(path).read()
            self.assertEqual(reopened["preferred_mode"], "proxy")
            self.assertGreater(reopened["generation"], 0)
            self.assertNotIn("proxy.test", repr(reopened))


if __name__ == "__main__":
    unittest.main()
