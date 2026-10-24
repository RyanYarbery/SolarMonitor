"""Push notifications over ntfy with per-key cooldowns shared through SQLite.

Configure with SOLAR_NTFY_URL (full topic URL such as https://ntfy.sh/my-solar)
and optionally SOLAR_NTFY_TOKEN. Nothing is sent when the URL is unset.

Cooldowns live in the alert_state table so the collector and the aggregator
(which runs in a separate process) do not both fire for the same condition.
"""

import json
import threading
import urllib.error
import urllib.request

import settings
from timeutil import now_utc, parse_iso, to_iso

PRIORITY = {"info": "default", "warning": "high", "error": "urgent"}


def enabled() -> bool:
    return bool(settings.NTFY_URL)


def _post(title: str, message: str, severity: str, tags=()):
    body = message.encode("utf-8")
    headers = {
        "Title": title,
        "Priority": PRIORITY.get(severity, "default"),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if settings.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {settings.NTFY_TOKEN}"
    req = urllib.request.Request(settings.NTFY_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError) as exc:
        print(f"ntfy send failed: {exc}", flush=True)
        return False


def send(title: str, message: str, severity: str = "info", tags=(), background=True):
    """Send unconditionally (no cooldown). Returns False when disabled."""
    if not enabled():
        return False
    if background:
        threading.Thread(target=_post, args=(title, message, severity, tags), daemon=True).start()
        return True
    return _post(title, message, severity, tags)


def _state(conn, key):
    row = conn.execute("SELECT last_sent, active FROM alert_state WHERE key = ?", (key,)).fetchone()
    return (parse_iso(row[0]), bool(row[1])) if row else (None, False)


def _set_state(conn, key, active, touch=True):
    now = to_iso(now_utc())
    if touch:
        conn.execute(
            "INSERT INTO alert_state (key, last_sent, active) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_sent = excluded.last_sent, active = excluded.active",
            (key, now, int(active)),
        )
    else:
        conn.execute(
            "INSERT INTO alert_state (key, last_sent, active) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET active = excluded.active",
            (key, now, int(active)),
        )


def raise_alert(conn, key, title, message, severity="warning", tags=(), cooldown=None):
    """Send unless the same key fired within the cooldown. Marks the key active.

    Caller is responsible for the transaction (use `with conn:`).
    """
    cooldown = settings.ALERT_COOLDOWN if cooldown is None else cooldown
    last_sent, _ = _state(conn, key)
    if last_sent is not None and (now_utc() - last_sent).total_seconds() < cooldown:
        _set_state(conn, key, active=True, touch=False)
        return False
    _set_state(conn, key, active=True)
    return send(title, message, severity, tags)


def clear_alert(conn, key, title=None, message=None, tags=("white_check_mark",)):
    """If the key is active, mark it cleared and optionally send a recovery note."""
    _, active = _state(conn, key)
    if not active:
        return False
    _set_state(conn, key, active=False, touch=False)
    if title:
        return send(title, message or "", "info", tags)
    return True


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)
