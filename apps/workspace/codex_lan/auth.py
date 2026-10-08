"""CODEX: Host-managed provider credentials, never exposed to browser sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from pathlib import Path

from .provider_usage import ProviderUsage


class AuthProvider:
    def __init__(self, codex_home=None, codex=None, route_state_path=None):
        self.home = Path(codex_home or Path.home() / ".codex")
        self.codex = codex or shutil.which("codex") or str(Path.home() / ".local/bin/codex")
        self.lock = asyncio.Lock()
        self.last_refresh = 0.0
        self.provider_usage = ProviderUsage(self.home, self.codex, route_state_path=route_state_path)

    async def rate_limits(self, force=False):
        return await self.provider_usage.read(force=force)

    async def close(self):
        await self.provider_usage.close()

    def _read(self):
        try:
            data = json.loads((self.home / "auth.json").read_text())
            tokens = data["tokens"]
            access = tokens["access_token"]
            claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "==="))
            auth = claims.get("https://api.openai.com/auth", {})
            account = tokens.get("account_id") or auth["chatgpt_account_id"]
            result = {"accessToken": access, "chatgptAccountId": account}
            if auth.get("chatgpt_plan_type"):
                result["chatgptPlanType"] = auth["chatgpt_plan_type"]
            return result, float(claims.get("exp", 0))
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Server Codex sign-in is unavailable. Sign in on the host first.") from None

    async def tokens(self, force=False):
        async with self.lock:
            result, expires = self._read()
            if (force or expires < time.time() + 300) and time.monotonic() - self.last_refresh > 30:
                await self._refresh()
                self.last_refresh = time.monotonic()
                result, expires = self._read()
            if expires < time.time() + 10:
                raise RuntimeError("Server Codex sign-in expired. Sign in on the host again.")
            return result

    async def _refresh(self):
        env = await self.provider_usage.connection_environment(dict(os.environ, CODEX_HOME=str(self.home)), scope="authentication")
        process = await asyncio.create_subprocess_exec(
            self.codex, "app-server", "--stdio", env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=8 * 1024 * 1024,
        )

        async def request(identifier, method, params):
            process.stdin.write((json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Server authentication service disconnected.")
                packet = json.loads(line)
                if packet.get("id") == identifier:
                    if "error" in packet:
                        raise RuntimeError("Server authentication refresh failed; sign in on the host again.")
                    return packet.get("result")

        async def handshake():
            await request(1, "initialize", {"clientInfo": {"name": "codex_lan_auth", "version": "0.1.0"}})
            process.stdin.write(b'{"method":"initialized"}\n')
            await request(2, "account/read", {"refreshToken": True})

        try:
            try:
                await asyncio.wait_for(handshake(), timeout=7)
            except asyncio.TimeoutError:
                await self.provider_usage.gateway.report_failure(scope="authentication")
                raise RuntimeError("Server authentication refresh timed out.") from None
            except OSError:
                await self.provider_usage.gateway.report_failure(scope="authentication")
                raise RuntimeError("Server authentication service disconnected.") from None
            await self.provider_usage.gateway.report_success(scope="authentication")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
