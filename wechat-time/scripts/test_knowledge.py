import unittest

import wechat_bridge


class KnowledgeCaptureTests(unittest.TestCase):
    def test_removes_only_edge_markers(self):
        cases = {
            "knowledge 研究 knowledge graphs": "研究 knowledge graphs",
            "研究 knowledge graphs knowledge": "研究 knowledge graphs",
            "知识：学习知识图谱": "学习知识图谱",
            "学习知识图谱，知识": "学习知识图谱",
            "知识 学习知识图谱 知识": "学习知识图谱",
            "KNOWLEDGE: knowledge matters, 知识": "knowledge matters",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(wechat_bridge._extract_knowledge(message), expected)

    def test_internal_words_are_not_commands(self):
        for message in ("学习知识图谱", "I study knowledge graphs", "知识图谱很有用"):
            with self.subTest(message=message):
                self.assertIsNone(wechat_bridge._extract_knowledge(message))

    def test_empty_marker_is_rejected_by_caller(self):
        self.assertEqual(wechat_bridge._extract_knowledge("知识"), "")
        self.assertEqual(wechat_bridge._extract_knowledge("knowledge 知识"), "")


if __name__ == "__main__":
    unittest.main()
