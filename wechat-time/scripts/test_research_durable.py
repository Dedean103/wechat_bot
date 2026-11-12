import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import wechat_bridge as bridge


class DurableResearchTests(unittest.TestCase):
    def make_bot(self):
        bot = bridge.Bridge.__new__(bridge.Bridge)
        bot.account = {"account_id": "acct"}
        bot.state = {"research_jobs": {}, "seen": [], "sync_buf": "", "context_token": ""}
        bot._research_lock = threading.RLock()
        bot.task_q = bridge.deque()
        bot._task_cond = threading.Condition()
        bot.save_state = Mock()
        bot.send_weixin = Mock(return_value=True)
        bot.send_file = Mock(return_value=True)
        bot._send_research_message = Mock(return_value=True)
        self._memory_patch = patch.object(bridge, "_append_memory", Mock())
        self._memory_patch.start()
        self.addCleanup(self._memory_patch.stop)
        self._load_memory_patch = patch.object(bridge, "_load_memory", Mock(return_value=""))
        self._load_memory_patch.start()
        self.addCleanup(self._load_memory_patch.stop)
        self._knowledge_prompt_patch = patch.object(bridge, "_knowledge_prompt", Mock(return_value=""))
        self._knowledge_prompt_patch.start()
        self.addCleanup(self._knowledge_prompt_patch.stop)
        self._knowledge_patch = patch.object(bridge, "_append_knowledge", Mock())
        self._knowledge_patch.start()
        self.addCleanup(self._knowledge_patch.stop)
        return bot

    def test_registration_is_durable_and_queued_once(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("帮我用小红书调研头盔", "xhs")
        self.assertTrue(task_id.startswith("research-"))
        self.assertTrue(bot._research_queue_item(task_id))
        self.assertFalse(bot._research_queue_item(task_id))
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["account_id"], "acct")
        self.assertEqual(bot.task_q[0]["research_task_id"], task_id)

    def test_research_execution_claim_is_atomic_and_respects_backoff(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        self.assertIsNotNone(bot._research_mark_running(task_id))
        self.assertIsNone(bot._research_mark_running(task_id))
        bot._research_fail(task_id, "model_execution", "timeout")
        self.assertIsNone(bot._research_mark_running(task_id))

    def test_timeout_turn_enters_retry_wait_with_checkpoint(self):
        bot = self.make_bot()
        bot._chat_active = 1
        bot.backend = "codex"
        bot.backend_cfg = {}
        bot.account = {"account_id": "acct"}
        bot._running_task = {}
        text = "帮我用小红书调研头盔"
        with patch.object(bridge, "classify_message", return_value="medium"), \
                patch.object(bridge, "_load_memory", return_value=""), \
                patch.object(bridge, "_should_attach_knowledge", return_value=False), \
                patch.object(bridge, "agent_reply", return_value=(None, None)):
            bot._process_one(text)
        job = bot._find_research_job(text)
        self.assertEqual(job["status"], "retry_wait")
        self.assertEqual(job["attempts"], 1)
        self.assertTrue(job["next_retry_ts"])

    def test_checkpoint_persists_session_partial_and_specific_failure_stage(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_checkpoint(task_id, stage="backend", error="workspace routing discovery timed out",
                                 session_id="sid-1", partial="关键发现 A")
        bot._research_checkpoint(task_id, stage="session", session_id="sid-1", partial="进度")
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["session_id"], "sid-1")
        self.assertEqual(job["failure_stage"], "model_routing")
        self.assertTrue(any("关键发现 A" in item["text"] for item in job["partial_findings"]))

    def test_execution_failures_keep_retrying_after_five_minutes(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        job = bot.state["research_jobs"][task_id]
        job["max_attempts"] = 3  # Legacy persisted limit must not stop recovery.
        for attempt in range(1, 6):
            bot._research_mark_running(task_id)
            with patch.object(bridge.time, "time", return_value=1800000000):
                bot._research_fail(task_id, "model_execution", "timeout")
            self.assertEqual(job["status"], "retry_wait")
            self.assertEqual(job["attempts"], attempt)
            self.assertEqual(bot._research_ts(job["next_retry_ts"]), 1800000300)
            self.assertIsNone(bot._research_mark_running(task_id))
            self.assertFalse(bot._research_queue_item(task_id))
            bot._research_transition(task_id, "queued", reason="test retry due")
        bot._send_research_message.assert_not_called()

    def test_scheduler_waits_until_due_and_cancellation_stops_recovery(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_mark_running(task_id, "checkpoint-session")
        bot._research_checkpoint(task_id, partial="已读两篇笔记")
        job = bot.state["research_jobs"][task_id]
        with patch.object(bridge.time, "time", return_value=1800000000):
            bot._research_fail(task_id, "browser_page", "timeout")
        for now, expected_count in ((1800000299, 0), (1800000300, 1), (1800000301, 1)):
            with patch.object(bridge.time, "time", return_value=now), \
                    patch.object(bridge.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    bot._research_retry_loop()
            self.assertEqual(len(bot.task_q), expected_count)
        self.assertEqual(job["session_id"], "checkpoint-session")
        self.assertEqual(job["partial_findings"][0]["text"], "已读两篇笔记")
        bot.task_q.clear()
        bot._research_mark_running(task_id)
        bot._research_fail(task_id, "browser_page", "timeout")
        bot._cancel_research_jobs()
        with patch.object(bridge.time, "time", return_value=2800000000), \
                patch.object(bridge.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                bot._research_retry_loop()
        bot._restore_research_jobs()
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(bot.task_q)

    def test_restart_recovers_running_job_without_resuming_helper(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_mark_running(task_id, "sid-1")
        bot.task_q.clear()
        bot._restore_research_jobs()
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "retry_wait")
        self.assertEqual(job["session_id"], "sid-1")
        self.assertFalse(bot.task_q)
        due = bot._research_ts(job["next_retry_ts"])
        self.assertAlmostEqual(due - bridge.time.time(), 300, delta=2)
        bot._restore_research_jobs()
        self.assertEqual(bot._research_ts(job["next_retry_ts"]), due)
        self.assertFalse(bot.task_q)

    def test_delivery_retry_reuses_archived_paths(self):
        bot = self.make_bot()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        archive = Path(temp.name)
        md = archive / "report.md"
        txt = archive / "report.txt"
        md.write_text("# 结论\n推荐 A\n\n# 调研范围\n范围\n\n# 参考来源\n来源\n", encoding="utf-8")
        txt.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "archived", archive_paths=[str(md), str(txt)],
                                 summary="摘要", delivery={"summary": "pending", "file": "pending", "completion": "pending"})
        with patch.object(bridge, "_is_complete_research_report", return_value=True):
            outcome = bot._deliver_research_report("", "调研测试", task_id=task_id)
        self.assertTrue(outcome.success)
        self.assertEqual(bot.state["research_jobs"][task_id]["archive_paths"], [str(md), str(txt)])
        bot._research_transition(task_id, "delivered", reason="done")
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "delivered")

    def test_cancellation_suppresses_retry_queue(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._cancel_research_jobs()
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(bot._research_queue_item(task_id))

    def test_delivery_failure_is_bounded_and_keeps_archive(self):
        bot = self.make_bot()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        archive = Path(temp.name)
        md = archive / "report.md"
        txt = archive / "report.txt"
        body = "# 结论\n推荐 A\n\n# 调研范围\n范围\n\n# 参考来源\n来源\n"
        md.write_text(body, encoding="utf-8")
        txt.write_text(body, encoding="utf-8")
        bot.send_file.return_value = False
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "archived", archive_paths=[str(md), str(txt)],
                                 summary="摘要", delivery={"summary": "pending", "file": "pending", "completion": "pending"})
        with patch.object(bridge, "_is_complete_research_report", return_value=True):
            for _ in range(bridge.RESEARCH_MAX_DELIVERY_ATTEMPTS):
                outcome = bot._deliver_research_report("", "调研测试", task_id=task_id)
                bot._research_delivery_failure(task_id, outcome)
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["delivery_attempts"], bridge.RESEARCH_MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(job["archive_paths"], [str(md), str(txt)])
        self.assertEqual(job["failure_notification"]["state"], "sent")

    def test_state_roundtrip_recovers_crashed_running_job_once(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state_file = root / "state.json"
        data_dir = root / "data"
        accounts = root / "accounts"
        accounts.mkdir(parents=True)
        with patch.object(bridge, "STATE_FILE", state_file), \
                patch.object(bridge, "DATA_DIR", data_dir):
            bot = self.make_bot()
            bot.accounts_dir = accounts
            bot.save_state = bridge.Bridge.save_state.__get__(bot)
            task_id = bot._create_research_job("调研测试", "web")
            bot._research_mark_running(task_id)
            bot.state["research_jobs"][task_id]["attempts"] = 3
            bot.save_state()
            loaded = bridge.json.loads(state_file.read_text(encoding="utf-8"))
            restored = self.make_bot()
            restored.accounts_dir = accounts
            restored.state = loaded
            restored.task_q = bridge.deque()
            restored._restore_research_jobs()
        job = restored.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "retry_wait")
        self.assertEqual(job["attempts"], 3)
        self.assertEqual(job["session_id"], None)
        restored._send_research_message.assert_not_called()
        self.assertFalse(restored.task_q)
        with patch.object(bridge.time, "time", return_value=restored._research_ts(job["next_retry_ts"])):
            restored._restore_research_jobs()
            restored._restore_research_jobs()
        self.assertEqual(len(restored.task_q), 1)
        self.assertIsNotNone(restored._research_mark_running(task_id))
        self.assertEqual(job["attempts"], 4)

    def test_bridge_init_deduplicates_legacy_research_queues(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        data = root / "data"
        accounts = data / "accounts"
        accounts.mkdir(parents=True)
        account_file = data / "account.json"
        state_file = data / "state.json"
        account_file.write_text('{"account_id":"acct","user_id":"user","token":"t","base_url":"http://example"}', encoding="utf-8")
        task_id = "research-legacy12345678"
        request = "帮我用小红书调研头盔"
        state = {
            "sync_buf": "", "context_token": "", "boss_chat": "user",
            "codex_session": None, "session_account": "acct", "seen": [], "seen_fps": [],
            "sessions": [], "pending_replies": [request], "processing": [request],
            "task_queue": [{"research_task_id": task_id, "text": request}],
            "research_jobs": {task_id: {
                "task_id": task_id, "request": request, "created_ts": "2026-10-27T00:00:00+00:00",
                "account_id": "acct", "route": "xhs", "status": "running", "attempts": 1,
                "max_attempts": 3, "next_retry_ts": None, "session_id": "sid",
                "partial_findings": [], "archive_paths": [], "archive_target": None,
                "summary": "", "failure_stage": "", "failure_reason": "",
                "queue_enqueued": True, "delivery": {"summary": "pending", "file": "pending", "completion": "pending"},
                "failure_notification": {"state": "none"}, "history": []}}
        }
        state_file.write_text(__import__("json").dumps(state), encoding="utf-8")
        with patch.object(bridge, "DATA_DIR", data), patch.object(bridge, "ACCOUNT_FILE", account_file), \
                patch.object(bridge, "STATE_FILE", state_file), patch.object(bridge, "ACCOUNTS_DIR", accounts), \
                patch.object(bridge, "_load_backend", return_value=("generic", {})):
            bot = bridge.Bridge()
        self.assertFalse(bot.reply_q)
        research_items = [item for item in bot.task_q if isinstance(item, dict) and item.get("research_task_id") == task_id]
        self.assertEqual(len(research_items), 0)
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "retry_wait")

    def test_followup_status_does_not_invoke_reminder_or_model(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("深圳洗窗帘公司", "web")
        bot._research_transition(task_id, "running", reason="test", override_terminal=True)
        bot._understand_reminder = Mock(side_effect=AssertionError("reminder model must not run"))
        self.assertTrue(bot._handle_research_followup("深圳洗窗帘有结果了吗"))
        self.assertIn(task_id, bot.send_weixin.call_args.args[0])
        self.assertIn("研究中", bot.send_weixin.call_args.args[0])

    def test_followup_retry_restarts_failed_job_without_new_report_request(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "failed", reason="legacy exhausted execution")
        bot.send_weixin.reset_mock()
        self.assertTrue(bot._handle_research_followup(f"重试 {task_id}"))
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "queued")
        self.assertTrue(any(isinstance(item, dict) and item.get("research_task_id") == task_id for item in bot.task_q))

    def test_followup_with_two_jobs_requests_task_id(self):
        bot = self.make_bot()
        first = bot._create_research_job("深圳洗窗帘公司", "web")
        second = bot._create_research_job("深圳洗窗帘价格", "web")
        self.assertTrue(bot._handle_research_followup("有结果了吗"))
        message = bot.send_weixin.call_args.args[0]
        self.assertIn(first, message)
        self.assertIn(second, message)

    def test_handle_update_routes_followup_before_reminder_model(self):
        bot = self.make_bot()
        bot.account = {"account_id": "acct", "user_id": "user"}
        bot.state.update({"seen": [], "boss_chat": "user", "seen_fps": {}, "context_token": ""})
        task_id = bot._create_research_job("深圳洗窗帘公司", "web")
        bot._research_transition(task_id, "running", reason="test", override_terminal=True)
        bot._understand_reminder = Mock(side_effect=AssertionError("reminder model must not run"))
        bot.send_typing = Mock()
        msg = {"from_user_id": "user", "message_id": "m1", "message_type": 1,
               "item_list": [{"type": 1, "text_item": {"text": "深圳洗窗帘有结果了吗"}}]}
        bot.handle_update(msg)
        self.assertIn("研究中", bot.send_weixin.call_args.args[0])
        bot._understand_reminder.assert_not_called()

    def test_archive_target_is_stable_for_task(self):
        target_a = bridge._research_archive_target("深圳洗窗帘公司", "research-deadbeef")
        target_b = bridge._research_archive_target("深圳洗窗帘公司", "research-deadbeef")
        self.assertEqual(target_a, target_b)
        self.assertIn("research-deadbeef", target_a.name)

    def test_research_outbox_does_not_enter_generic_retry_queue(self):
        bot = self.make_bot()
        bot.state["boss_chat"] = "user"
        bot.account["user_id"] = "user"
        bot._send_research_message = bridge.Bridge._send_research_message.__get__(bot)
        bot._send_message = Mock(return_value="error")
        with patch.object(bridge, "_save_to_retry_queue") as retry, patch.object(bridge.time, "sleep"):
            self.assertFalse(bot._send_research_message("失败通知", "research-test", "failure"))
        retry.assert_not_called()

    def test_restart_marks_inflight_delivery_ambiguous_without_auto_resend(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "delivering", reason="test send claim",
                                 delivery_attempts=1,
                                 delivery={"summary": "sending", "file": "pending", "completion": "pending"})
        bot.task_q.clear()
        bot._restore_research_jobs()
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "delivery_ambiguous")
        self.assertFalse(bot.task_q)
        self.assertTrue(bot._retry_research_job(task_id))
        self.assertEqual(job["status"], "archived")
        self.assertTrue(bot.task_q)

    def test_delivery_claim_is_exclusive_and_unavailable_claim_does_not_mutate(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "archived", archive_paths=[])
        self.assertIsNotNone(bot._research_mark_delivery(task_id))
        before = dict(bot.state["research_jobs"][task_id])
        outcome = bot._deliver_research_report("", "调研测试", task_id=task_id)
        self.assertFalse(outcome.handled)
        self.assertEqual(outcome.stage, "delivery_claim")
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "delivering")
        self.assertEqual(bot.state["research_jobs"][task_id]["delivery_attempts"],
                         before["delivery_attempts"])
        bot._research_delivery_failure(task_id, outcome)
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "delivering")
        bot._send_research_message.assert_not_called()

    def test_restart_recovers_complete_reserved_target_before_archive_transition(self):
        bot = self.make_bot()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "reserved.md"
        target.write_text("# 结论\n推荐 A\n\n# 调研范围\n范围\n\n# 参考来源\n来源\n", encoding="utf-8")
        task_id = bot._create_research_job("调研测试", "web")
        bot.state["research_jobs"][task_id]["archive_target"] = str(target)
        bot._research_mark_running(task_id)
        bot.task_q.clear()
        bot._restore_research_jobs()
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "archived")
        self.assertIn(str(target), job["archive_paths"])
        self.assertTrue(any(item.get("research_task_id") == task_id for item in bot.task_q))

    def test_post_archive_exception_retries_delivery_without_model_attempt(self):
        bot = self.make_bot()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        md = Path(temp.name) / "report.md"
        md.write_text("# 结论\n推荐 A\n\n# 调研范围\n范围\n\n# 参考来源\n来源\n", encoding="utf-8")
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "delivering", archive_paths=[str(md)], delivery_attempts=1)
        bot._research_post_archive_failure(task_id, "save_state crashed after archive")
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "archived")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["failure_stage"], "archive_processing")

    def test_failure_notification_ack_is_ignored_after_explicit_retry_supersedes_send(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "failed", reason="timeout")

        def supersede(*args, **kwargs):
            bot._retry_research_job(task_id)
            return True

        bot._send_research_message.side_effect = supersede
        self.assertFalse(bot._notify_research_failure(task_id))
        job = bot.state["research_jobs"][task_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["failure_notification"]["state"], "none")
        self.assertEqual(job["failure_notification"]["generation"], 1)

    def test_failure_notification_records_claimed_part_states(self):
        bot = self.make_bot()
        bot.state.update({"boss_chat": "user", "context_token": ""})
        bot.account["user_id"] = "user"
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "failed", reason="timeout")
        bot._send_research_message = bridge.Bridge._send_research_message.__get__(bot)
        bot._send_message = Mock(return_value="ok")
        self.assertTrue(bot._notify_research_failure(task_id))
        note = bot.state["research_jobs"][task_id]["failure_notification"]
        self.assertEqual(note["state"], "sent")
        self.assertTrue(note["parts"])
        self.assertTrue(all(part["state"] == "sent" for part in note["parts"]))
        self.assertEqual({part["generation"] for part in note["parts"]}, {note["generation"]})

    def test_failure_text_uses_utc_suffix_and_task_partial_location(self):
        bot = self.make_bot()
        task_id = bot._create_research_job("调研测试", "web")
        bot._research_transition(task_id, "failed", reason="timeout")
        job = bot.state["research_jobs"][task_id]
        text = bot._research_failure_text(job)
        self.assertIn(job["created_ts"].split("+")[0], text)
        self.assertIn("Z", text)
        self.assertIn(f"{bridge.STATE_FILE}/{task_id}", text)

    def test_bridge_init_skips_terminal_job_from_legacy_processing_text(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        data = root / "data"
        accounts = data / "accounts"
        accounts.mkdir(parents=True)
        account_file = data / "account.json"
        state_file = data / "state.json"
        account_file.write_text('{"account_id":"acct","user_id":"user","token":"t","base_url":"http://example"}', encoding="utf-8")
        task_id = "research-terminal123456"
        request = "帮我用小红书调研头盔"
        state = {
            "sync_buf": "", "context_token": "", "boss_chat": "user",
            "codex_session": None, "session_account": "acct", "seen": [], "seen_fps": {},
            "sessions": [], "pending_replies": [], "processing": [request], "task_queue": [],
            "research_jobs": {task_id: {
                "task_id": task_id, "request": request, "created_ts": "2026-10-27T00:00:00+00:00",
                "account_id": "acct", "route": "xhs", "status": "delivered", "attempts": 1,
                "max_attempts": 3, "next_retry_ts": None, "session_id": "sid",
                "partial_findings": [], "archive_paths": [], "archive_target": None,
                "summary": "", "failure_stage": "", "failure_reason": "",
                "queue_enqueued": False, "delivery": {"summary": "sent", "file": "sent", "completion": "sent"},
                "failure_notification": {"state": "none"}, "history": []}}
        }
        state_file.write_text(__import__("json").dumps(state), encoding="utf-8")
        with patch.object(bridge, "DATA_DIR", data), patch.object(bridge, "ACCOUNT_FILE", account_file), \
                patch.object(bridge, "STATE_FILE", state_file), patch.object(bridge, "ACCOUNTS_DIR", accounts), \
                patch.object(bridge, "_load_backend", return_value=("generic", {})):
            bot = bridge.Bridge()
        self.assertEqual(len(bot.state["research_jobs"]), 1)
        self.assertEqual(bot.state["research_jobs"][task_id]["status"], "delivered")
        self.assertFalse(bot.reply_q)
        self.assertFalse(bot.task_q)


if __name__ == "__main__":
    unittest.main()
