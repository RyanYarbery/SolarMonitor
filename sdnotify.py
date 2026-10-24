"""Minimal sd_notify(3) client, standard library only.

Used by the collector so systemd can supervise it with Type=notify and
WatchdogSec=. Every call is a no-op when NOTIFY_SOCKET is unset, so the
code runs unchanged outside systemd.
"""

import os
import socket


def notify(state: str) -> bool:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(state.encode())
        return True
    except OSError:
        return False


def ready():
    return notify("READY=1")


def watchdog():
    return notify("WATCHDOG=1")


def status(text: str):
    return notify(f"STATUS={text}")
