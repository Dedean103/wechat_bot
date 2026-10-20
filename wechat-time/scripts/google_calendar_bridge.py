"""Call the connected Google Calendar through Codex; no model or extra OAuth secrets.

One process per operation, bounded by the caller's timeout. Writes are never
automatically retried: an interrupted response may already have created an event.
"""
import json
import signal
import subprocess
import sys


class CalendarClient:
    def __init__(self, command, cwd):
        self.proc = subprocess.Popen(command + ["app-server"], cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.seq = 0
        try:
            self.rpc("initialize", {"clientInfo": {"name": "wechat-calendar", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
            self.thread = self.rpc("thread/start", {"cwd": cwd, "ephemeral": True})["thread"]["id"]
        except Exception:
            self.close()
            raise

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.seq += 1
        seq = self.seq
        self.send({"id": seq, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("Codex calendar connection closed")
            message = json.loads(line)
            if "method" in message and "id" in message:
                # Interactive approval/authentication is never silently granted.
                self.send({"id": message["id"], "error": {"code": -32601,
                           "message": "Interactive requests are unsupported by the calendar bridge"}})
                raise RuntimeError("Google Calendar requires interactive authorization")
            if message.get("id") != seq:
                continue
            if "error" in message:
                raise RuntimeError("Codex calendar RPC failed")
            return message["result"]

    def call(self, action, arguments):
        if action not in {"list_calendars", "create_event", "read_event", "delete_event"}:
            raise ValueError("Unsupported calendar action")
        result = self.rpc("mcpServer/tool/call", {"threadId": self.thread,
                          "server": "codex_apps", "tool": "google_calendar." + action,
                          "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("Google Calendar rejected the operation")
        data = result.get("structuredContent")
        if data is None:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        data = json.loads(item["text"])
                        break
                    except (ValueError, KeyError):
                        pass
        return data

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def main():
    def timed_out(signum, frame):
        raise TimeoutError("Calendar operation timed out")
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(80)
    request = json.load(sys.stdin)
    client = CalendarClient(request["command"], request["cwd"])
    try:
        data = client.call(request["action"], request["arguments"])
        print(json.dumps({"data": data}))
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not leak calendar content, tokens, or server diagnostics into logs.
        print(json.dumps({"error": "Google Calendar operation failed"}))
        sys.exit(1)
