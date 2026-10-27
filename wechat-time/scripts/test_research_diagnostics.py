import io
from pathlib import Path
import tempfile
import unittest

from research_diagnostics import capture_diagnostics, failure_stage, redact_diagnostic


class DiagnosticsTests(unittest.TestCase):
    def test_secrets_and_signed_urls_are_redacted(self):
        raw = ('Authorization: Bearer abc123\n{"access_token": "private", '
               '"api_key":"sk-example123", "cookie":"session=private; x=1"}\n'
               'Cookie: session=private; secondary=hidden\n--api-key cli-secret\n'
               'https://alice:pass123@example.com\n'
               'https://www.xiaohongshu.com/explore/abc?xsec_token=signed&source=web')
        redacted = redact_diagnostic(raw)
        for secret in ("abc123", "private", "sk-example123", "signed", "source=web", "hidden", "cli-secret", "pass123"):
            self.assertNotIn(secret, redacted)
        self.assertIn("/explore/abc?[REDACTED]", redacted)

    def test_capture_is_redacted_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            thread = capture_diagnostics(io.StringIO("helper failure token=private\n"),
                                         Path(directory), "helper")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            saved = (Path(directory) / "helper.log").read_text()
            self.assertIn("helper failure", saved)
            self.assertNotIn("private", saved)

    def test_stage_classification(self):
        cases = {"workspace routing discovery timed out": "model_routing",
                 "Page.MustWait context deadline exceeded": "browser_page",
                 "助手 ready 超时": "helper_startup", "归档失败": "archive_processing",
                 "send_file failed": "wechat_delivery", "unknown": "model_execution",
                 "tools/call timed out": "model_execution"}
        for error, stage in cases.items():
            self.assertEqual(failure_stage(error), stage)

    def test_overlong_secret_line_does_not_leak_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = 'token="' + 'a' * 17000 + 'hidden-tail"\nnext line\n'
            thread = capture_diagnostics(io.StringIO(raw), Path(directory), "helper")
            thread.join(timeout=2)
            saved = (Path(directory) / "helper.log").read_text()
            self.assertNotIn("hidden-tail", saved)
            self.assertNotIn("aaaa", saved)
            self.assertIn("next line", saved)


if __name__ == "__main__":
    unittest.main()
