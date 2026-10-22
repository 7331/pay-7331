"""A webhook receiver for e2e/polygon_fork_flow.py's API token and webhook scenario and its Discord scenario: a
standard-library HTTP server run in a container on the e2e stack's network (the host firewall drops container traffic
to host ports).

Every POST is appended to ``RECEIVED_FILE`` as one JSON line (its path, the three ``webhook-*`` headers, the body as
sent and the status answered). The first request carrying a ``webhook-id`` whose body's ``type`` is in
``WEBHOOK_RECEIVER_FAIL_ONCE_TYPES`` (comma-separated) is answered 500, so the sender's retry shows; everything else is
answered 204. The scenario reads the file with ``docker exec``.

It also stands in for Discord's Execute Webhook (the API's ``PAY_E2E_DISCORD_API_BASE_URL``): a POST to
``/api/v10/webhooks/{id}/{token}`` is recorded with ``"discord": true``; a webhook id in
``DISCORD_RECEIVER_DELETED_WEBHOOK_IDS`` is answered a bare 404 (Discord's ``404: Not Found``, code 0: the API must
stop on any 404, not only on ``Unknown Webhook``), the first message whose content contains
``DISCORD_RECEIVER_RATE_LIMIT_ONCE_TEXT`` is answered 429 with ``retry_after`` as Discord does, the rest 204.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

RECEIVED_FILE = Path("/tmp/webhook_received.jsonl")  # noqa: S108  # a throwaway container's own /tmp
LISTEN_PORT = 8080
FAIL_ONCE_TYPES = {
    event_type for event_type in os.environ.get("WEBHOOK_RECEIVER_FAIL_ONCE_TYPES", "").split(",") if event_type
}
failed_webhook_ids: set[str] = set()
received_file_lock = threading.Lock()
DISCORD_PATH_PREFIX = "/api/v10/webhooks/"
DISCORD_DELETED_WEBHOOK_IDS = {
    webhook_id for webhook_id in os.environ.get("DISCORD_RECEIVER_DELETED_WEBHOOK_IDS", "").split(",") if webhook_id
}
DISCORD_RATE_LIMIT_ONCE_TEXT = os.environ.get("DISCORD_RECEIVER_RATE_LIMIT_ONCE_TEXT", "")
DISCORD_RETRY_AFTER_SECONDS = 1.5
rate_limited_discord_contents: set[str] = set()


def answer_discord_message(path: str, *, body: str) -> tuple[int, bytes]:
    """Discord's answer to Execute Webhook: (status, JSON body or nothing)."""
    discord_webhook_id = path.removeprefix(DISCORD_PATH_PREFIX).split("/", 1)[0]
    if discord_webhook_id in DISCORD_DELETED_WEBHOOK_IDS:
        return 404, json.dumps({"message": "404: Not Found", "code": 0}).encode()
    try:
        content = str(json.loads(body).get("content", ""))
    except ValueError:
        content = ""
    if DISCORD_RATE_LIMIT_ONCE_TEXT and DISCORD_RATE_LIMIT_ONCE_TEXT in content and not rate_limited_discord_contents:
        rate_limited_discord_contents.add(content)
        rate_limit_body = {"message": "You are being rate limited.", "retry_after": DISCORD_RETRY_AFTER_SECONDS,
                           "global": False}  # fmt: skip
        return 429, json.dumps(rate_limit_body).encode()
    return 204, b""


class WebhookReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        if self.path.startswith(DISCORD_PATH_PREFIX):
            self.answer_discord(body)
            return
        webhook_id = self.headers.get("webhook-id", "")
        try:
            event_type = str(json.loads(body).get("type", ""))
        except ValueError:
            event_type = ""
        with received_file_lock:
            fail_this_time = event_type in FAIL_ONCE_TYPES and webhook_id not in failed_webhook_ids
            if fail_this_time:
                failed_webhook_ids.add(webhook_id)
            answered_status = 500 if fail_this_time else 204
            record = {
                "path": self.path,
                "host": self.headers.get("Host", ""),
                "headers": {
                    name: self.headers.get(name, "")
                    for name in ("webhook-id", "webhook-timestamp", "webhook-signature")
                },
                "body": body,
                "answered_status": answered_status,
            }
            with RECEIVED_FILE.open("a") as received_file:
                received_file.write(json.dumps(record) + "\n")
        self.send_response(answered_status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def answer_discord(self, body: str) -> None:
        with received_file_lock:
            answered_status, answer_body = answer_discord_message(self.path.split("?", 1)[0], body=body)
            record = {"discord": True, "path": self.path, "host": self.headers.get("Host", ""),
                      "user_agent": self.headers.get("User-Agent", ""), "body": body, "answered_status": answered_status}  # fmt: skip
            with RECEIVED_FILE.open("a") as received_file:
                received_file.write(json.dumps(record) + "\n")
        self.send_response(answered_status)
        if answered_status == 429:
            self.send_header("Retry-After", "2")
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset-After", str(DISCORD_RETRY_AFTER_SECONDS))
        if answer_body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer_body)))
        self.end_headers()
        self.wfile.write(answer_body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, ARG002  # http.server's signature
        return


if __name__ == "__main__":
    RECEIVED_FILE.touch()
    # The container's own interface on the stack's network.
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), WebhookReceiverHandler).serve_forever()  # noqa: S104
