"""CODEX: Shared WebSocket liveness policy and secret-free transport diagnostics."""

from __future__ import annotations


LAN_WEBSOCKET_HEARTBEAT_SECONDS = 30.0
EXPECTED_WEBSOCKET_CLOSE_CODES = {1000, 1001, 1008}


def device_id_suffix(device_id) -> str:
    value = str(device_id or "")
    return value[-8:] if value else "unknown"


def websocket_diagnostic(websocket=None, failure=None) -> dict[str, object]:
    close_code = getattr(websocket, "close_code", None) if websocket is not None else None
    if isinstance(close_code, bool) or not isinstance(close_code, int):
        close_code = None
    websocket_error = None
    exception = getattr(websocket, "exception", None) if websocket is not None else None
    if callable(exception):
        try:
            error = exception()
        except Exception as exc:
            websocket_error = type(exc).__name__
        else:
            if error is not None:
                websocket_error = type(error).__name__
    return {
        "close_code": close_code,
        "websocket_error": websocket_error or "none",
        "failure_type": type(failure).__name__ if failure is not None else "none",
    }


def format_transport_diagnostic(role, diagnostic, *, attempt=None, retry_seconds=None) -> str:
    fields = [
        ("role", str(role)),
        ("close_code", str(diagnostic.get("close_code") or "none")),
        ("websocket_error", str(diagnostic.get("websocket_error") or "none")),
        ("failure_type", str(diagnostic.get("failure_type") or "none")),
    ]
    if attempt is not None:
        fields.append(("attempt", str(max(1, int(attempt)))))
    if retry_seconds is not None:
        fields.append(("retry_in_seconds", f"{max(0.0, float(retry_seconds)):.1f}"))
    return " ".join(f"{key}={value}" for key, value in fields)
