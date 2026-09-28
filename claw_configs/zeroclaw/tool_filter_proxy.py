#!/usr/bin/env python3
import http.server, json, os, urllib.request, ssl, sys, socketserver, threading
from datetime import datetime, timezone
from pathlib import Path

REAL_BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OPENROUTER_BASE_URL", "")).strip().rstrip("/")
if not REAL_BASE:
    raise SystemExit("OPENROUTER_BASE_URL is not set; export your API base URL or pass it as the first argument.")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18090
USAGE_LOG = sys.argv[3] if len(sys.argv) > 3 else "/tmp/proxy_usage.jsonl"

_usage_lock = threading.Lock()

ALLOWED_TOOLS = {
    "shell",
    "file_read", "file_write", "file_edit",
    "glob_search", "content_search", "git_operations",
}

IS_DEEPSEEK = "deepseek.com" in REAL_BASE
IS_OPENROUTER = "openrouter.ai" in REAL_BASE


class FilterProxy(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
            if "tools" in data:
                data["tools"] = [t for t in data["tools"]
                                 if t.get("function", {}).get("name", "") in ALLOWED_TOOLS]
                if not data["tools"]:
                    del data["tools"]
                    if "tool_choice" in data:
                        del data["tool_choice"]
            if "input" in data and "messages" not in data:
                input_val = data.pop("input")
                messages = []
                if isinstance(input_val, str):
                    messages = [{"role": "user", "content": input_val}]
                elif isinstance(input_val, list):
                    for item in input_val:
                        if isinstance(item, str):
                            messages.append({"role": "user", "content": item})
                        elif isinstance(item, dict):
                            messages.append(item)
                data["messages"] = messages
                for key in ["instructions", "previous_response_id", "truncation"]:
                    data.pop(key, None)

            if IS_DEEPSEEK and "messages" in data:
                for msg in data["messages"]:
                    if msg.get("role") == "assistant":
                        if "reasoning_content" not in msg:
                            msg["reasoning_content"] = ""
                if "reasoning_effort" not in data:
                    data["reasoning_effort"] = "xhigh"

            client_effort = data.get("reasoning_effort")
            if IS_OPENROUTER and "reasoning_effort" not in data:
                data["reasoning_effort"] = "xhigh"
            sent_effort = data.get("reasoning_effort")


            data["stream"] = False
            body = json.dumps(data, ensure_ascii=False).encode()
        except (json.JSONDecodeError, KeyError):
            pass

        target_url = f"{REAL_BASE}/chat/completions"
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "transfer-encoding", "accept-encoding"):
                headers[k] = v
        headers["Host"] = REAL_BASE.split("//")[1].split("/")[0]
        headers["Content-Length"] = str(len(body))
        headers["Accept-Encoding"] = "identity"

        req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")
        ctx = ssl.create_default_context()
        try:
            resp = urllib.request.urlopen(req, context=ctx, timeout=300)
            resp_body = resp.read()

            try:
                resp_data = json.loads(resp_body)

                api_usage = resp_data.get("usage", {})
                if api_usage:
                    log_entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "client_ip": self.client_address[0],
                        "model": resp_data.get("model", ""),
                        "usage": api_usage,
                        "client_effort": client_effort,
                        "sent_effort": sent_effort,
                    }
                    with _usage_lock:
                        with open(USAGE_LOG, "a") as f:
                            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

                for choice in resp_data.get("choices", []):
                    msg = choice.get("message", {})
                    if "reasoning" in msg:
                        msg.pop("reasoning", None)
                    if "reasoning_details" in msg:
                        msg.pop("reasoning_details", None)
                    choice.pop("native_finish_reason", None)
                resp_body = json.dumps(resp_data, ensure_ascii=False).encode()
            except (json.JSONDecodeError, KeyError):
                pass

            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            err_msg = str(e).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(err_msg)))
            self.end_headers()
            self.wfile.write(err_msg)

    def log_message(self, *a): pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


print(f"Tool filter proxy on :{PORT} → {REAL_BASE} (allowing: {ALLOWED_TOOLS})"
      f"{' [DeepSeek mode]' if IS_DEEPSEEK else ''}", file=sys.stderr, flush=True)
ThreadedHTTPServer(("0.0.0.0", PORT), FilterProxy).serve_forever()
