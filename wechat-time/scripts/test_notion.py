import unittest
from datetime import date
from unittest.mock import patch

import wechat_bridge


def child_page(page_id, title):
    return {"id": page_id, "type": "child_page", "child_page": {"title": title}}


class NotionWeekPageTests(unittest.TestCase):
    def test_cross_month_week_can_live_under_end_month(self):
        tracker = [child_page("aug", "2026-08"), child_page("sep", "2026-09")]
        pages = {
            "aug": [child_page("old", "0824-0830")],
            "sep": [child_page("target", "0831-0906")],
        }
        with patch.object(wechat_bridge, "_notion_children", side_effect=lambda page_id: tracker if page_id == "tracker" else pages[page_id]), \
             patch.object(wechat_bridge, "NOTION_HABIT_TRACKER_ID", "tracker"):
            result = wechat_bridge._notion_week_page(date(2026, 8, 31))
        self.assertEqual(result["id"], "target")

    def test_cross_year_week_uses_both_year_month_pages(self):
        tracker = [child_page("dec", "2026-12"), child_page("jan", "2027-01")]
        pages = {
            "dec": [],
            "jan": [child_page("target", "1228-0103")],
        }
        with patch.object(wechat_bridge, "_notion_children", side_effect=lambda page_id: tracker if page_id == "tracker" else pages[page_id]), \
             patch.object(wechat_bridge, "NOTION_HABIT_TRACKER_ID", "tracker"):
            result = wechat_bridge._notion_week_page(date(2027, 1, 1))
        self.assertEqual(result["id"], "target")


if __name__ == "__main__":
    unittest.main()
