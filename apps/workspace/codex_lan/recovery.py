"""CODEX: Recognize transport failures and expose a small public connection state."""

import math
import time


NETWORK_ERRORS = {"httpconnectionfailed", "responsestreamconnectionfailed", "responsestreamdisconnected", "responsetoomanyfailedattempts"}
CONTINUE_AFTER_CONNECTION = (
    "The connection was interrupted. Continue the existing request from the saved conversation. "
    "Check completed tool results and any command still running before proceeding; do not repeat completed side effects. "
    "Preserve the user's constraints and finish the remaining work."
)


def network_error(error):
    if not isinstance(error, dict):
        return False
    if error.get("httpStatusCode") in {401, 403, 429}:
        return False
    info = error.get("codexErrorInfo")
    if isinstance(info, dict):
        for name, detail in info.items():
            if str(name).lower() in NETWORK_ERRORS:
                status = detail.get("httpStatusCode") if isinstance(detail, dict) else None
                return status is None or status in {408, 425, 500, 502, 503, 504}
        return False
    if isinstance(info, str) and info.lower() in NETWORK_ERRORS:
        return True
    if info not in (None, "other"):
        return False
    text = str(error.get("message", "")).lower()
    return text.startswith(("stream disconnected before completion", "error sending request for url", "connection reset by peer", "network is unreachable"))


def connection_snapshot(value):
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    if state not in {"connecting", "reconnecting", "switching", "connected", "resuming"}:
        return None
    result = {"state": state, "updated_at": time.time()}
    if value.get("mode") in {"direct", "proxy"}:
        result["mode"] = value["mode"]
    if value.get("preferred_mode") in {"direct", "proxy"}:
        result["preferred_mode"] = value["preferred_mode"]
    for key, maximum in (("attempt", 5), ("attempt_limit", 100), ("switches", 1000000), ("retry_in_seconds", 120)):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            result[key] = max(0, min(maximum, number))
    return result


def connection_text(connection):
    state = connection.get("state")
    route = {"direct": "direct connection", "proxy": "SOCKS5 proxy"}.get(connection.get("mode"), "connection")
    if state == "connected":
        return "Connected to the model"
    if state == "switching":
        return "Trying another network route via " + route
    if state == "resuming":
        return "Attempting to continue the response via " + route
    saved = connection.get("preferred_mode") in {None, connection.get("mode")}
    if state == "reconnecting" and saved:
        return "Retrying the saved connection via " + route
    return ("Trying another network route via " if state == "reconnecting" else "Connecting via ") + route
