"""CODEX: Typed request context keys shared by middleware and endpoint modules."""

from aiohttp import web


DEVICE = web.RequestKey("device", dict)
