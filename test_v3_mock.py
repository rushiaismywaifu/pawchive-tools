#!/usr/bin/env python3
"""用本地 mock server 驗證 v2 client 的寫入路徑與邊界情境（不打正式站）。"""
import http.server, json, threading, socketserver, sys, io, contextlib

import pawchive_client_v3 as pc

HITS = {}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _count(self, key):
        HITS[key] = HITS.get(key, 0) + 1
        return HITS[key]

    def handle_one(self, method):
        p = self.path
        # 1. GET flag 已被標記 → 規格寫 200 且 content:{} → 空 body
        if p.endswith("/flag") and method == "GET":
            self.send_response(200); self.send_header("Content-Length","0"); self.end_headers(); return
        # 2. POST flag 成功 → 201 空 body
        if p.endswith("/flag") and method == "POST":
            self.send_response(201); self.send_header("Content-Length","0"); self.end_headers(); return
        # 3. favorites 未認證 → 302 導向登入頁（規格明載）
        if p.startswith("/favorites/"):
            self.send_response(302); self.send_header("Location","/account/login"); self.send_header("Content-Length","0"); self.end_headers(); return
        if p.startswith("/account/login"):
            body = b"<!doctype html><html><body>login form</body></html>"
            self.send_response(200); self.send_header("Content-Type","text/html")
            self.send_header("Content-Length",str(len(body))); self.end_headers()
            self.wfile.write(body); return
        # 4. 重試測試：前兩次 503，第三次成功
        if p.startswith("/flaky"):
            n = self._count("flaky")
            if n < 3:
                self.send_response(503); self.send_header("Content-Length","0"); self.end_headers(); return
            body = json.dumps({"ok": n}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body))); self.end_headers()
            self.wfile.write(body); return
        # 5. POST 在 5xx 後重試 → 觀察是否重複送出
        if p.startswith("/writeflaky"):
            n = self._count("writeflaky")
            if n < 2:
                self.send_response(500); self.send_header("Content-Length","0"); self.end_headers(); return
            self.send_response(201); self.send_header("Content-Length","0"); self.end_headers(); return
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

    def do_GET(self): self.handle_one("GET")
    def do_POST(self): self.handle_one("POST")
    def do_DELETE(self): self.handle_one("DELETE")


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
pc.BASE = f"http://127.0.0.1:{port}"

def probe(label, fn):
    try:
        r = fn()
        print(f"{label:<46} → 回傳 {r!r}")
    except Exception as e:
        print(f"{label:<46} → {type(e).__name__}: {str(e)[:110]}")

print("--- 情境 A：GET flag，貼文『已被標記』(200 + 空 body) ---")
probe("get_flag() 已標記", lambda: pc.is_flagged("fanbox", "1", "2"))

print("\n--- 情境 B：POST flag 成功 (201 + 空 body) ---")
probe("flag_post()", lambda: pc.flag_post("fanbox", "1", "2"))

print("\n--- 情境 C：session 失效 → 302 導向登入頁 ---")
probe("favorite_post()  [POST]", lambda: pc.favorite_post("bad", "fanbox", "1", "2"))
probe("unfavorite_post() [DELETE]", lambda: pc.unfavorite_post("bad", "fanbox", "1", "2"))
probe("favorite_creator() [POST]", lambda: pc.favorite_creator("bad", "fanbox", "1"))
probe("unfavorite_creator() [DELETE]", lambda: pc.unfavorite_creator("bad", "fanbox", "1"))

print("\n--- 情境 D：503 兩次後成功，確認重試有效 ---")
probe("GET /flaky", lambda: pc.get("/flaky"))
print(f"  server 實際收到 /flaky 請求次數：{HITS.get('flaky')}")

print("\n--- 情境 E：POST 遇 500 重試，確認是否重複送出 ---")
probe("POST /writeflaky", lambda: pc.request("POST", "/writeflaky", empty_ok=True))
print(f"  server 實際收到 /writeflaky 請求次數：{HITS.get('writeflaky')}  ← 非冪等寫入被送了幾次")

srv.shutdown()
