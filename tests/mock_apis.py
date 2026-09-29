#!/usr/bin/env python3
"""Offline stand-in for Anthropic, Telegram, Facebook Graph and X APIs.

Used only by tests/run_e2e.sh. Every request is appended to /work/requests.jsonl
so the test can assert on exactly what n8n sent. OAuth 1.0a signatures on X
calls are verified with the fake consumer/token secrets from the test .env.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LOG", "/work/requests.jsonl")
CONSUMER_SECRET = os.environ.get("MOCK_X_CONSUMER_SECRET", "test-consumer-secret")
TOKEN_SECRET = os.environ.get("MOCK_X_TOKEN_SECRET", "test-token-secret")
FB_TOKEN = os.environ.get("MOCK_FB_TOKEN", "test-fb-token")
BEARER = os.environ.get("MOCK_X_BEARER", "test-bearer")
ANTHROPIC_KEY = os.environ.get("MOCK_ANTHROPIC_KEY", "test-anthropic-key")
FAIL_FB = os.environ.get("MOCK_FAIL_FB") == "1"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
msg_counter = [1000]

SITE_HTML = """<!doctype html><html><head><title>Mock Org &amp; Friends</title>
<style>.x{color:red}</style><script>var tracking = 'IGNORE_ME';</script></head>
<body><nav>Home | About | Donate</nav>
<h1>Together for a greener island</h1>
<p>We run weekly reef clean-ups with 120 volunteers.</p>
<footer>Copyright footer text</footer></body></html>"""
SITE_POSTS = [
    {"date": "2026-09-20T10:00:00", "link": "http://mock:9000/site/clean-beach-day",
     "title": {"rendered": "Clean Beach Day &#8211; 28 September"},
     "excerpt": {"rendered": "<p>Join 300 volunteers at Hulhumale beach.</p>"}},
]


def pct(s):
    return urllib.parse.quote(str(s), safe="~-._")


def verify_oauth1(method, url, query, auth):
    if not auth.startswith("OAuth "):
        return "missing OAuth header"
    params = dict(
        (k, urllib.parse.unquote(v.strip('"')))
        for k, v in re.findall(r'(\w+)=("[^"]*")', auth)
    )
    sig = params.pop("oauth_signature", None)
    allp = [(k, v) for k, v in params.items() if k.startswith("oauth_")]
    allp += [(k, v) for k, vs in query.items() for v in vs]
    norm = "&".join(sorted(f"{pct(k)}={pct(v)}" for k, v in allp))
    base = "&".join([method.upper(), pct(url), pct(norm)])
    key = f"{pct(CONSUMER_SECRET)}&{pct(TOKEN_SECRET)}"
    good = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "ok" if sig == good else f"bad signature (got {sig}, want {good})"


def claude_reply(body):
    schema_props = body.get("output_config", {}).get("format", {}).get("schema", {}).get("properties", {})
    user = json.dumps(body.get("messages", []))
    if "verdict" in schema_props:
        high = "love" in user.lower()
        out = {"score": 9 if high else 2, "verdict": "HIGH_VALUE" if high else "SPAM",
               "sentiment": "positive" if high else "neutral",
               "reason": "Genuine customer praise." if high else "Promotional spam."}
    else:
        out = {"twitter_copy": "Mock tweet: small steps, big wins. #YourBrand",
               "fb_copy": "Mock Facebook story <with> & special chars.\n\nLearn more: https://example.com",
               "image_idea": "A sunrise over a desk",
               "source_link": ("http://mock:9000/site/clean-beach-day" if "Clean Beach Day" in user else "")}
    return {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [{"type": "thinking", "thinking": "", "signature": "x"},
                        {"type": "text", "text": json.dumps(out)}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 10}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        body = {}
        if "json" in ctype and raw:
            body = json.loads(raw)
        elif "x-www-form-urlencoded" in ctype:
            body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        elif "multipart" in ctype:
            body = {"_multipart_fields": re.findall(r'name="([^"]+)"', raw.decode("latin-1")),
                    "_bytes": len(raw)}
        rec = {"method": self.command, "path": path, "query": query, "body": body,
               "headers": {k.lower(): v for k, v in self.headers.items()}}
        resp = self.route(rec, query)
        rec["status"] = resp[0]
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self._send(*resp) if len(resp) == 2 else self._send(resp[0], raw=resp[1], ctype=resp[2])

    do_GET = do_POST = handle_any

    def route(self, rec, query):
        p, b, h = rec["path"], rec["body"], rec["headers"]
        # ---- test helpers ----
        # ---- a fake organisation website (WordPress-like) ----
        if p == "/site/":
            return (200, SITE_HTML.encode(), "text/html; charset=utf-8")
        if p == "/wp-json/wp/v2/posts":
            return (200, SITE_POSTS)
        if p == "/img.png":
            return (200, PNG, "image/png")
        if p == "/health":
            return (200, {"ok": True})
        # ---- Anthropic ----
        if p == "/v1/messages":
            if h.get("x-api-key") != ANTHROPIC_KEY or h.get("anthropic-version") != "2023-06-01":
                return (401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
            return (200, claude_reply(b))
        # ---- Telegram ----
        m = re.match(r"^/bot([^/]+)/(\w+)$", p)
        if m:
            method = m.group(2)
            params = dict(b) if isinstance(b, dict) else {}
            params.update({k: v[0] for k, v in query.items()})
            rec["tg_method"] = method
            if method == "getFile":
                return (200, {"ok": True, "result": {"file_id": params.get("file_id"), "file_path": "photos/f.png"}})
            if method in ("sendMessage", "sendPhoto", "editMessageText"):
                msg_counter[0] += 1
                return (200, {"ok": True, "result": {
                    "message_id": int(params.get("message_id") or msg_counter[0]),
                    "chat": {"id": int(params.get("chat_id", 0) or 0)},
                    "text": params.get("text", "")}})
            return (200, {"ok": True, "result": True})
        if re.match(r"^/file/bot[^/]+/", p):
            return (200, PNG, "image/png")
        # ---- Facebook ----
        m = re.match(r"^/v[\d.]+/(\w+)/(feed|photos)$", p)
        if m:
            if query.get("access_token", [""])[0] != FB_TOKEN:
                return (400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
            if FAIL_FB:
                return (400, {"error": {"message": "(#200) Mock FB failure", "code": 200}})
            if m.group(2) == "photos":
                return (200, {"id": "photo_1", "post_id": f"{m.group(1)}_222"})
            return (200, {"id": f"{m.group(1)}_111"})
        # ---- X ----
        full = "http://mock:9000" + p
        if p.startswith("/2/users/") and p.endswith("/mentions"):
            if h.get("authorization") != f"Bearer {BEARER}":
                return (401, {"title": "Unauthorized"})
            return (200, {
                "data": [
                    {"id": "9001", "text": "@YourBrand I love your product, it saved my week!", "author_id": "501",
                     "created_at": "2026-09-27T10:00:00.000Z"},
                    {"id": "9002", "text": "@YourBrand FREE CRYPTO click here", "author_id": "502",
                     "created_at": "2026-09-27T10:01:00.000Z"},
                ],
                "includes": {"users": [
                    {"id": "501", "username": "happy_customer", "name": "Happy", "public_metrics": {"followers_count": 1200}},
                    {"id": "502", "username": "spambot", "name": "Spam", "public_metrics": {"followers_count": 3}},
                ]},
                "meta": {"newest_id": "9002", "result_count": 2},
            })
        if p in ("/2/tweets", "/2/media/upload") or (p.startswith("/2/users/") and p.endswith("/retweets")):
            rec["oauth1"] = verify_oauth1(rec["method"], full, query, h.get("authorization", ""))
            if rec["oauth1"] != "ok":
                return (401, {"title": "Unauthorized", "detail": rec["oauth1"]})
            if p == "/2/media/upload":
                return (200, {"data": {"id": "777", "media_key": "3_777"}})
            if p == "/2/tweets":
                return (201, {"data": {"id": "1234567890", "text": b.get("text")}})
            return (200, {"data": {"retweeted": True}})
        return (404, {"error": "no mock for " + p})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), H).serve_forever()
