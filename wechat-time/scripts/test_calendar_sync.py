import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import wechat_bridge as bridge


class CalendarSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in {"DATA_DIR": root, "REMINDER_DB_FILE": root / "reminders.db",
                            "REMINDER_CONFIG_FILE": root / "config.json"}.items():
            p = patch.object(bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        bridge.REMINDER_CONFIG_FILE.write_text(json.dumps({"google_calendar_id": "test@example.com"}))
        self.bot = object.__new__(bridge.Bridge)
        self.bot.state = {"boss_chat": "test-user"}
        self.bot.account = {}
        self.bot.send_weixin = Mock()
        self.intent = {"action": "create", "title": "test reminder",
                       "datetime": (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")}

    def row(self):
        conn = bridge._reminder_db()
        try:
            return dict(conn.execute("SELECT * FROM reminders").fetchone())
        finally:
            conn.close()

    def test_both_calendars_and_local_timer_survive_independent_failures(self):
        for icloud_error, google_error in [(None, None), ("iCloud failed", None), (None, "Google failed"),
                                            ("iCloud failed", "Google failed")]:
            with self.subTest(icloud=icloud_error, google=google_error):
                conn = bridge._reminder_db()
                with conn:
                    conn.execute("DELETE FROM reminders")
                conn.close()
                with patch.object(bridge, "_create_icloud_reminder", return_value=("icloud-ref", icloud_error)), \
                     patch.object(bridge, "_create_google_reminder", return_value=("google-id", google_error)) as google:
                    self.assertTrue(self.bot._handle_reminder_action(self.intent))
                    google.assert_called_once_with(self.intent["title"], self.intent["datetime"], "test@example.com")
                row = self.row()
                self.assertEqual(row["status"], "scheduled")
                self.assertEqual(row["calendar_status"], "failed" if icloud_error else "created")
                self.assertEqual(row["google_status"], "unconfirmed" if google_error else "created")

    def test_google_time_duration_alarm_and_no_retry_on_uncertain_write(self):
        with patch.object(bridge, "_google_calendar_call", return_value={"id": "event-id"}) as call:
            self.assertEqual(bridge._create_google_reminder("title", "2026-11-01T23:50:00", "cal"), ("event-id", None))
            action, args = call.call_args.args
            self.assertEqual(action, "create_event")
            self.assertEqual(args["start_time"], "2026-11-01T23:50:00+08:00")
            self.assertEqual(args["end_time"], "2026-11-02T00:20:00+08:00")
            self.assertEqual(args["reminders"], {"use_default": False, "overrides": [{"method": "popup", "minutes": 10}]})
            self.assertEqual(args["attendees"], [])
        with patch.object(bridge, "_google_calendar_call", side_effect=TimeoutError) as call:
            event_id, error = bridge._create_google_reminder("title", self.intent["datetime"], "cal")
            self.assertIsNone(event_id)
            self.assertTrue(error)
            call.assert_called_once()

    def test_cancel_uses_original_calendar_and_reports_remote_failure(self):
        with patch.object(bridge, "_create_icloud_reminder", return_value=("icloud-ref", None)), \
             patch.object(bridge, "_create_google_reminder", return_value=("google-id", None)):
            self.bot._handle_reminder_action(self.intent)
        bridge.REMINDER_CONFIG_FILE.write_text('{"google_calendar_id":"changed"}')
        with patch.object(bridge, "_delete_icloud_reminder", return_value=True), \
             patch.object(bridge, "_delete_google_reminder", return_value=False) as delete:
            self.bot._handle_reminder_action({"action": "cancel", "title": self.intent["title"]})
            delete.assert_called_once_with("google-id", "test@example.com")
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertEqual(self.row()["google_status"], "cancel_failed")
        self.assertTrue(any("Google" in c.args[0] and "手动检查" in c.args[0] for c in self.bot.send_weixin.call_args_list))

    def test_existing_reminders_are_preserved_without_google_backfill(self):
        conn = sqlite3.connect(bridge.REMINDER_DB_FILE)
        conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, user_id TEXT, title TEXT, due_at_utc TEXT, "
                     "timezone TEXT, calendar_event TEXT, status TEXT, created_at_utc TEXT, delivered_at_utc TEXT, last_error TEXT)")
        conn.execute("INSERT INTO reminders (id, title, status) VALUES (1, 'old reminder', 'scheduled')")
        conn.commit()
        conn.close()
        row = self.row()
        self.assertEqual(row["title"], "old reminder")
        self.assertEqual(row["google_status"], "not_requested")
        self.assertEqual(row["google_event_id"], "")


if __name__ == "__main__":
    unittest.main()
