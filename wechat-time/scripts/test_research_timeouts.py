import queue
import threading
import unittest
from unittest.mock import Mock, patch

import wechat_bridge as bridge


class ResearchTimeoutTests(unittest.TestCase):
    def test_new_dispatch_resets_old_stop_but_preserves_durable_cancellation(self):
        bridge._cancel_event.set()
        with patch.object(bridge, "codex_reply", return_value=("hello", "sid")) as reply:
            self.assertEqual(bridge.agent_reply("codex", "new chat"), ("hello", "sid"))
            self.assertFalse(bridge._cancel_event.is_set())
            reply.reset_mock()
            bridge._cancel_event.set()
            self.assertEqual(bridge.agent_reply("codex", "cancelled research", "old",
                                                cancel_check=lambda: True), (None, "old"))
            reply.assert_not_called()

    def setUp(self):
        self.clock = 0.0
        for name, value in (("log", Mock()), ("_probe_codex_provider", Mock(return_value=True)),
                            ("_helper_ensure", Mock(return_value=True)),
                            ("_helper_restart", Mock()), ("_cancel_event", threading.Event())):
            p = patch.object(bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bridge.time, "monotonic", side_effect=lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bridge.time, "sleep", side_effect=self.advance)
        p.start()
        self.addCleanup(p.stop)

    def advance(self, seconds):
        self.clock += seconds

    def run_turn(self, active=False, deadline=12, inactivity=360):
        q = Mock()

        def get(timeout):
            self.advance(timeout)
            if active:
                return {"type": "activity"}
            raise queue.Empty

        q.get.side_effect = get
        with patch.object(bridge.queue, "Queue", return_value=q), \
                patch.object(bridge, "_helper_send", return_value=True) as send:
            reply = bridge.codex_reply("research", timeout=inactivity, deadline=deadline)
        return reply, [call.args[0] for call in send.call_args_list]

    def test_activity_does_not_extend_overall_deadline(self):
        reply, commands = self.run_turn(active=True)
        self.assertEqual(reply, (None, None))
        turns = [c for c in commands if c["type"] == "turn"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(commands[-1], {"type": "interrupt", "reqId": turns[0]["id"]})
        bridge._helper_restart.assert_not_called()
        self.assertEqual(bridge._turn_queues, {})

    def test_retries_share_deadline_and_interrupt_only_their_request(self):
        _, commands = self.run_turn(deadline=12, inactivity=3)
        turns = [c for c in commands if c["type"] == "turn"]
        interrupts = [c for c in commands if c["type"] == "interrupt"]
        self.assertEqual(len(turns), 2)
        self.assertEqual([c["reqId"] for c in interrupts], [c["id"] for c in turns])
        bridge._helper_restart.assert_not_called()

    def test_durable_cancel_during_retry_sleep_prevents_next_backend_turn(self):
        q = Mock()

        def get(timeout):
            self.advance(timeout)
            raise queue.Empty

        q.get.side_effect = get
        cancelled = {"value": False}

        def retry_sleep(seconds):
            self.advance(seconds)
            cancelled["value"] = True

        with patch.object(bridge.queue, "Queue", return_value=q), \
                patch.object(bridge, "_helper_send", return_value=True) as send, \
                patch.object(bridge.time, "sleep", side_effect=retry_sleep) as sleep:
            reply = bridge.codex_reply("research", timeout=3, deadline=60,
                                       cancel_check=lambda: cancelled["value"])
        self.assertEqual(reply, (None, None))
        turns = [call.args[0] for call in send.call_args_list
                 if call.args and call.args[0].get("type") == "turn"]
        self.assertEqual(len(turns), 1)
        self.assertTrue(sleep.called)

    def test_expired_deadline_never_starts_provider_or_turn(self):
        with patch.object(bridge, "_helper_send") as send:
            self.assertEqual(bridge.codex_reply("research", deadline=0), (None, None))
        bridge._probe_codex_provider.assert_not_called()
        send.assert_not_called()

    def test_short_delta_is_checkpointed_with_session_before_inactivity_failure(self):
        q = Mock()
        events = iter(({"type": "session", "threadId": "sid-new"},
                       {"type": "delta", "text": "关键发现：店 A"}))

        def get(timeout):
            self.advance(timeout)
            try:
                return next(events)
            except StopIteration:
                raise queue.Empty

        q.get.side_effect = get
        checkpoints = []
        with patch.object(bridge.queue, "Queue", return_value=q), \
                patch.object(bridge, "_helper_send", return_value=True):
            reply = bridge.codex_reply("research", timeout=3, deadline=10,
                                       checkpoint=lambda event: checkpoints.append(event))
        self.assertEqual(reply, (None, "sid-new"))
        self.assertTrue(any(e.get("session_id") == "sid-new" for e in checkpoints))
        self.assertTrue(any("关键发现" in e.get("partial", "") for e in checkpoints))

    def test_research_limits_do_not_depend_on_chat_complexity(self):
        for text, timeout, bounded in (("帮我用小红书调研头盔", 360, True),
                                       ("你好", 90, False)):
            with self.subTest(text=text):
                bot = bridge.Bridge.__new__(bridge.Bridge)
                bot.state = {}
                bot._chat_active = 1
                bot.backend = "codex"
                bot.backend_cfg = {}
                bot.account = {}
                bot.save_state = Mock()
                bot.send_weixin = Mock()
                with patch.object(bridge, "classify_message", return_value="medium"), \
                        patch.object(bridge, "_load_memory", return_value=""), \
                        patch.object(bridge, "_should_attach_knowledge", return_value=False), \
                        patch.object(bridge, "agent_reply", return_value=(None, None)) as reply:
                    bot._process_one(text)
                self.assertEqual(reply.call_args.kwargs["timeout"], timeout)
                self.assertEqual(reply.call_args.kwargs["deadline"], 1200 if bounded else None)

    def test_background_fallback_shares_research_deadline(self):
        bot = bridge.Bridge.__new__(bridge.Bridge)
        bot.state = {}
        bot._resume_task = None
        bot._task_preempt = threading.Event()
        bot._running_task = {}
        bot.backend = "codex"
        bot.backend_cfg = {}
        bot.save_state = Mock()
        bot.send_weixin = Mock()
        with patch.object(bridge, "_load_memory", return_value=""), \
                patch.object(bridge, "_should_attach_knowledge", return_value=False), \
                patch.object(bridge, "agent_reply", return_value=(None, None)) as reply:
            bot._process_task("帮我用小红书调研头盔")
        self.assertEqual(reply.call_count, 2)
        for call in reply.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 360)
            self.assertEqual(call.kwargs["deadline"], 1200)


if __name__ == "__main__":
    unittest.main()
