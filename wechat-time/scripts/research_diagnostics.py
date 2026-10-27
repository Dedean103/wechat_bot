"""Bounded, redacted subprocess diagnostics for bridge backend recovery."""

import logging
from logging.handlers import RotatingFileHandler
import re
import threading


def redact_diagnostic(value):
    text = str(value or "")
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?im)^(\s*(?:set-cookie|cookie)\s*:)[^\r\n]*', r'\1 [REDACTED]', text)
    # Signed note links and API URLs can contain credentials in any query field.
    text = re.sub(r'(https?://[^\s?<>"\']+)\?[^\s<>"\']+', r'\1?[REDACTED]', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*["\']?)(?:bearer\s+)?[^\s,"\'}]+',
                  r'\1[REDACTED]', text)
    text = re.sub(
        r'''(?ix)(["']?(?:[\w-]*(?:token|secret|password|cookie|api[_-]?key)[\w-]*)["']?\s*[:=]\s*)(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s,;}]+)''',
        r'\1[REDACTED]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)(--[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*\s+)\S+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    return text


def failure_stage(error, default="model_execution"):
    text = str(error or "").lower()
    if any(word in text for word in ("workspace routing", "workspace-routing", "routing discovery")):
        return "model_routing"
    if any(word in text for word in ("browser", "page.mustwait", "search_feeds", "get_feed_detail", "searchaction.search")):
        return "browser_page"
    if any(word in text for word in ("helper", "助手", "app-server", "初始化", "ready 超时")):
        return "helper_startup"
    if any(word in text for word in ("archive", "归档", "完整报告", "txt 副本")):
        return "archive_processing"
    if any(word in text for word in ("wechat", "微信", "send_file", "delivery")):
        return "wechat_delivery"
    return default


def capture_diagnostics(stream, directory, name):
    """Drain a PIPE on a daemon thread; store only redacted, rotating output."""
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / (name + ".log"), maxBytes=512 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s UTC %(message)s"))
    # Formatter timestamps follow the bridge host's timezone explicitly.
    import time
    handler.formatter.converter = time.gmtime
    logger = logging.Logger("bridge-diagnostics-" + name)
    logger.addHandler(handler)

    def drain():
        try:
            while True:
                line = stream.readline(16384)
                if not line:
                    break
                truncated = not line.endswith(b"\n" if isinstance(line, bytes) else "\n") and len(line) >= 16384
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                # Discard continuation chunks: otherwise a credential spanning
                # the chunk boundary could appear without its redaction key.
                if truncated:
                    while True:
                        tail = stream.readline(16384)
                        if not tail or tail.endswith(b"\n" if isinstance(tail, bytes) else "\n"):
                            break
                logger.warning("%s", redact_diagnostic(line.rstrip()))
        finally:
            stream.close()
            handler.close()

    thread = threading.Thread(target=drain, name="diagnostics-" + name, daemon=True)
    thread.start()
    return thread
