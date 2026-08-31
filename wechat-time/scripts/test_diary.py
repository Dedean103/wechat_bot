import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import wechat_bridge


class DiaryCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.diary_file = Path(self.temp_dir.name) / "diary.md"
        self.original_file = wechat_bridge.DIARY_FILE
        wechat_bridge.DIARY_FILE = self.diary_file

    def tearDown(self):
        wechat_bridge.DIARY_FILE = self.original_file
        self.temp_dir.cleanup()

    def test_extracts_english_and_chinese_keywords(self):
        self.assertEqual(wechat_bridge._extract_diary("diary 今天完成了部署"), "今天完成了部署")
        self.assertEqual(wechat_bridge._extract_diary("日记：值得记下来"), "值得记下来")
        self.assertEqual(wechat_bridge._extract_diary("DIARY"), "")
        self.assertIsNone(wechat_bridge._extract_diary("普通聊天"))

    def test_inserts_newest_entry_at_top_of_existing_date(self):
        self.diary_file.write_text(
            "## 2026-08-31\n\n- 08:00: older thought\n\n## 2026-08-30\n\n- 20:00: yesterday\n",
            encoding="utf-8",
        )
        timestamp = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)
        wechat_bridge._append_diary_local("new thought", timestamp)
        content = self.diary_file.read_text(encoding="utf-8")
        self.assertLess(content.index("new thought"), content.index("older thought"))
        self.assertLess(content.index("older thought"), content.index("yesterday"))

    def test_new_date_section_is_inserted_before_older_dates(self):
        self.diary_file.write_text("## 2026-08-30\n\n- 20: yesterday\n", encoding="utf-8")
        timestamp = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)
        wechat_bridge._append_diary_local("today", timestamp)
        content = self.diary_file.read_text(encoding="utf-8")
        self.assertLess(content.index("## 2026-08-31"), content.index("## 2026-08-30"))

    @patch.object(wechat_bridge, "_notion_diary_manager")
    def test_notion_manager_receives_same_thought_and_timestamp(self, manager_factory):
        manager = Mock()
        manager_factory.return_value = manager
        timestamp = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)
        wechat_bridge._append_diary_notion("same thought", timestamp, "diary-page")
        manager.append_message.assert_called_once_with("same thought", timestamp)


if __name__ == "__main__":
    unittest.main()
