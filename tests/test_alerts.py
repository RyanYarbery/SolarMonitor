import os
import socket
import tempfile
import unittest
from unittest import mock

import alerts
import sdnotify
import settings
from tests.helpers import cleanup_db, fresh_db


class AlertCooldownTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()
        settings.NTFY_URL = "https://ntfy.example/test"
        self.sent = []
        self.patcher = mock.patch.object(
            alerts, "send", side_effect=lambda *a, **k: self.sent.append(a) or True
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        settings.NTFY_URL = ""
        self.conn.close()
        cleanup_db(self.path)

    def test_cooldown_suppresses_repeat_but_clear_resets(self):
        with self.conn:
            self.assertTrue(alerts.raise_alert(self.conn, "k", "T", "m", cooldown=3600))
            self.assertFalse(alerts.raise_alert(self.conn, "k", "T", "m", cooldown=3600))
        self.assertEqual(len(self.sent), 1)

    def test_clear_only_when_active(self):
        with self.conn:
            self.assertFalse(alerts.clear_alert(self.conn, "nothing", "T"))
            alerts.raise_alert(self.conn, "k", "T", "m")
            self.assertTrue(alerts.clear_alert(self.conn, "k", "Cleared", "ok"))
            self.assertFalse(alerts.clear_alert(self.conn, "k", "Cleared", "ok"))

    def test_cooldown_expired_resends(self):
        with self.conn:
            alerts.raise_alert(self.conn, "k", "T", "m", cooldown=0)
            self.assertTrue(alerts.raise_alert(self.conn, "k", "T", "m", cooldown=0))


class AlertDisabledTests(unittest.TestCase):
    def test_disabled_when_url_empty(self):
        old = settings.NTFY_URL
        settings.NTFY_URL = ""
        try:
            self.assertFalse(alerts.enabled())
            self.assertFalse(alerts.send("t", "m", background=False))
        finally:
            settings.NTFY_URL = old


class AlertCliTests(unittest.TestCase):
    def test_test_flag_sends_when_enabled(self):
        old = settings.NTFY_URL
        settings.NTFY_URL = "https://ntfy.example/t"
        try:
            with mock.patch.object(alerts, "_post", return_value=True) as post:
                self.assertEqual(alerts.main(["--test"]), 0)
                post.assert_called_once()
        finally:
            settings.NTFY_URL = old

    def test_test_flag_disabled(self):
        old = settings.NTFY_URL
        settings.NTFY_URL = ""
        try:
            self.assertEqual(alerts.main(["--test"]), 1)
        finally:
            settings.NTFY_URL = old


class SdNotifyTests(unittest.TestCase):
    def test_noop_without_socket(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            self.assertFalse(sdnotify.watchdog())

    def test_sends_datagram(self):
        path = os.path.join(tempfile.mkdtemp(), "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(path)
        srv.settimeout(2)
        try:
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                self.assertTrue(sdnotify.ready())
                self.assertTrue(sdnotify.watchdog())
            self.assertEqual(srv.recv(64), b"READY=1")
            self.assertEqual(srv.recv(64), b"WATCHDOG=1")
        finally:
            srv.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
