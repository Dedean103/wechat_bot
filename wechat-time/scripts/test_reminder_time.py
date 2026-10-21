import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import wechat_bridge as bridge


TZ = ZoneInfo("Asia/Shanghai")


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 20, 18, 14, 42, tzinfo=TZ)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class ReminderTimeTests(unittest.TestCase):
    def test_nearest_future_clock_and_explicit_constraints(self):
        cases = [
            ("18:14", "九点半", "2026-10-20T21:30:00"),
            ("08:00", "九点半", "2026-10-20T09:30:00"),
            ("21:40", "九点半", "2026-10-21T09:30:00"),
            ("23:00", "两点", "2026-10-21T02:00:00"),
            ("13:00", "下午两点", "2026-10-20T14:00:00"),
            ("18:00", "明早九点", "2026-10-21T09:00:00"),
            ("09:30", "九点半", "2026-10-20T21:30:00"),
            ("11:00", "十二点半", "2026-10-20T12:30:00"),
            ("13:00", "十二点半", "2026-10-21T00:30:00"),
            ("18:14", "请在今天晚上九点半", "2026-10-20T21:30:00"),
            ("18:14", "明天上午九点半", "2026-10-21T09:30:00"),
            ("18:14", "21点30分", "2026-10-20T21:30:00"),
            ("18:14", "21:30", "2026-10-20T21:30:00"),
            ("18:14", "九点三刻", "2026-10-20T21:45:00"),
            ("18:14", "九点十五分三十秒", "2026-10-20T21:15:30"),
            ("23:59", "零点", "2026-10-21T00:00:00"),
        ]
        for current, clause, expected in cases:
            with self.subTest(current=current, clause=clause):
                now = datetime.fromisoformat("2026-10-20T" + current).replace(tzinfo=TZ)
                self.assertEqual(bridge._reminder_clock_time(clause + "提醒我事项", now), expected)

    def test_invalid_past_and_unsupported_clock_clauses_require_confirmation(self):
        now = FrozenDateTime.now(TZ)
        for clause in ("今天上午九点半", "今天下午两点", "明早晚上九点", "九点或者十点",
                       "二十五点", "九点六十分", "九点九十秒", "下周三九点", "今晚十二点"):
            with self.subTest(clause=clause), self.assertRaises(ValueError):
                bridge._reminder_clock_time(clause + "提醒我事项", now)

    def test_unrelated_formats_and_title_clock_words_are_not_overridden(self):
        for text in ("十分钟后提醒我事项", "提醒我买九点半的车票", "tomorrow at 9 remind me"):
            with self.subTest(text=text):
                self.assertIsNone(bridge._reminder_clock_time(text, FrozenDateTime.now(TZ)))

    def test_wrong_model_time_is_corrected_before_storage_and_both_calendars(self):
        bot = object.__new__(bridge.Bridge)
        bot.backend_cfg = {}
        bot.state = {"boss_chat": "test-user"}
        bot.account = {}
        bot.send_weixin = Mock()
        reply = json.dumps({"action": "create", "title": "茶壶和保温壶",
                            "datetime": "2026-10-21T09:30:00"})
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(bridge, "datetime", FrozenDateTime), \
             patch.object(bridge, "REMINDER_DB_FILE", Path(tmp) / "reminders.db"), \
             patch.object(bridge, "_load_reminder_config", return_value={"google_calendar_id": "test-cal"}), \
             patch.object(bridge, "codex_reply", return_value=(reply, None)), \
             patch.object(bridge, "_create_icloud_reminder", return_value=("icloud-id", None)) as icloud, \
             patch.object(bridge, "_create_google_reminder", return_value=("google-id", None)) as google:
            intent = bot._understand_reminder("九点半提醒我茶壶和保温壶")
            self.assertEqual(intent["datetime"], "2026-10-20T21:30:00")
            self.assertTrue(bot._handle_reminder_action(intent))
            conn = bridge._reminder_db()
            try:
                row = conn.execute("SELECT due_at_utc FROM reminders").fetchone()
                self.assertEqual(row[0], "2026-10-20T13:30:00+00:00")
            finally:
                conn.close()
            icloud.assert_called_once_with("茶壶和保温壶", "2026-10-20T21:30:00")
            google.assert_called_once_with("茶壶和保温壶", "2026-10-20T21:30:00", "test-cal")
            self.assertIn("2026-10-20 21:30:00", bot.send_weixin.call_args_list[0].args[0])

    def test_uncertain_or_invalid_intents_never_write_or_sync(self):
        bot = object.__new__(bridge.Bridge)
        bot.backend_cfg = {}
        bot.send_weixin = Mock()
        with patch.object(bridge, "datetime", FrozenDateTime), \
             patch.object(bridge, "_reminder_db") as db, \
             patch.object(bridge, "_create_icloud_reminder") as icloud, \
             patch.object(bridge, "_create_google_reminder") as google:
            for action, time in (("create", "2026-10-19T09:30:00"), ("create", "bad"), ("clarify", "")):
                self.assertTrue(bot._handle_reminder_action({"action": action, "datetime": time, "title": "事项"}))
            reply = json.dumps({"action": "create", "title": "事项", "datetime": "2026-10-21T09:30:00"})
            with patch.object(bridge, "codex_reply", return_value=(reply, None)):
                intent = bot._understand_reminder("今天上午九点半提醒我事项")
            self.assertEqual(intent["action"], "clarify")
            self.assertTrue(bot._handle_reminder_action(intent))
            db.assert_not_called()
            icloud.assert_not_called()
            google.assert_not_called()


if __name__ == "__main__":
    unittest.main()
