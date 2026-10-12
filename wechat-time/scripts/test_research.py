import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import wechat_bridge as bridge


REPORT = """# 测试报告
## 1. 结论先行
日常维护可以自己做，复杂问题交给专业店。
## 2. 调研范围
比较成本、效果和风险。
## 8. 参考笔记
### 教程
- [来源](https://example.com/report)
"""
SUMMARY = "自己维护省钱，专业店适合复杂问题。"
ENVELOPE = f"前置说明不属于报告\n【微信调研摘要】\n{SUMMARY}\n【报告正文】\n{REPORT}"


class ResearchArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.work = root / "work"
        self.memo = root / "memo"
        self.archive = self.memo / "investigation"
        self.work.mkdir()
        self.archive.mkdir(parents=True)
        for name, value in (("CODEX_WORK_DIR", str(self.work)),
                            ("AI_MEMO_DIR", self.memo),
                            ("RESEARCH_ARCHIVE_DIR", self.archive)):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = patch.object(bridge, "log").start()
        self.addCleanup(patch.stopall)
        self.knowledge = patch.object(bridge, "_append_knowledge").start()
        self.sender = Mock()

    def deliver(self, reply, files=()):
        return bridge.Bridge._deliver_research_report(self.sender, reply, "调研测试", files)

    def test_split_discards_preamble_and_summary(self):
        self.assertEqual(bridge._split_research_reply(ENVELOPE), (SUMMARY, REPORT.strip()))

    def test_body_marker_without_summary_and_crlf(self):
        reply = ("前置说明\n## 【报告正文】\n" + REPORT).replace("\n", "\r\n")
        self.assertEqual(bridge._split_research_reply(reply)[0], "")
        self.assertTrue(bridge._is_complete_research_report(bridge._split_research_reply(reply)[1]))

    def test_summary_marker_without_body_does_not_become_report(self):
        self.assertEqual(bridge._split_research_reply(f"【微信调研摘要】\n{SUMMARY}"), (SUMMARY, ""))

    def test_summary_and_empty_shell_are_rejected(self):
        for reply in (SUMMARY, f"【微信调研摘要】\n{SUMMARY}",
                      f"【微信调研摘要】\n{SUMMARY}\n【报告正文】",
                      "# 结论先行\n# 调研范围\n# 参考来源\n"):
            with self.subTest(reply=reply):
                self.sender.reset_mock()
                self.assertTrue(self.deliver(reply))
                self.assertEqual(list(self.archive.iterdir()), [])
                self.sender.send_file.assert_not_called()
                self.knowledge.assert_not_called()
                self.assertIn("归档失败", self.sender.send_weixin.call_args.args[0])

    def test_inline_report_wins_over_summary_file(self):
        source = self.work / "summary.md"
        source.write_text(SUMMARY)
        self.assertEqual(bridge._report_body_from_reply(ENVELOPE, [source]), (REPORT.strip(), None))

    def test_skips_invalid_file_and_recovers_complete_report(self):
        full = self.work / "report.md"
        thin = self.work / "summary.md"
        full.write_text(ENVELOPE)
        thin.write_text(SUMMARY)
        self.assertEqual(bridge._report_body_from_reply(SUMMARY, [full, thin]),
                         (REPORT.strip(), full))

    def test_file_outside_allowed_roots_is_ignored(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text(REPORT)
        self.assertEqual(bridge._report_body_from_reply(SUMMARY, [outside]), (SUMMARY, None))

    def test_delivery_archives_only_body_and_identical_txt(self):
        self.assertTrue(self.deliver(ENVELOPE))
        md, = self.archive.glob("*.md")
        self.assertEqual(md.read_text(), REPORT)
        self.assertEqual(md.read_bytes(), md.with_suffix(".txt").read_bytes())
        self.sender.send_file.assert_called_once_with(str(md.with_suffix(".txt")))
        self.assertEqual(self.sender.send_weixin.call_args_list[0].args[0], SUMMARY)
        self.knowledge.assert_called_once()

    def test_enveloped_archive_source_gets_clean_copy(self):
        source = self.archive / "original.md"
        source.write_text(ENVELOPE)
        self.assertTrue(self.deliver(SUMMARY, [source]))
        sent = Path(self.sender.send_file.call_args.args[0])
        self.assertNotEqual(sent, source.with_suffix(".txt"))
        self.assertEqual(sent.read_text(), REPORT)
        self.assertEqual(source.read_text(), ENVELOPE)

    def test_verified_archive_source_is_reused(self):
        source = self.archive / "original.md"
        source.write_text(REPORT)
        self.assertTrue(self.deliver(SUMMARY, [source]))
        self.assertEqual(list(self.archive.glob("*.md")), [source])
        self.sender.send_file.assert_called_once_with(str(source.with_suffix(".txt")))

    def test_save_rejects_summary_even_outside_delivery(self):
        self.assertIsNone(bridge._save_research_report(SUMMARY, "调研测试"))
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_failed_archive_does_not_send_file_or_record_completion(self):
        with patch.object(bridge, "_save_research_report", return_value=None):
            self.assertTrue(self.deliver(ENVELOPE))
        self.sender.send_file.assert_not_called()
        self.knowledge.assert_not_called()
        self.assertIn("归档失败", self.sender.send_weixin.call_args.args[0])

    def test_corrupt_txt_copy_is_removed(self):
        source = self.archive / "report.md"
        source.write_text(REPORT)
        with patch.object(bridge.shutil, "copyfile",
                          side_effect=lambda src, dst: Path(dst).write_text(SUMMARY)):
            self.assertIsNone(bridge._save_research_text_copy(source))
        self.assertFalse(source.with_suffix(".txt").exists())

    def test_temp_summary_is_not_archived_or_deleted(self):
        research = self.work / "research"
        research.mkdir()
        source = research / "summary.md"
        source.write_text(SUMMARY)
        self.assertIsNone(bridge._archive_temp_report_file(source, "调研测试"))
        self.assertTrue(source.exists())
        self.assertEqual(list(self.archive.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
