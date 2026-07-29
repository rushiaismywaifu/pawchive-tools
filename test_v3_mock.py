#!/usr/bin/env python3
"""
用本地 mock server 驗證 client 與下載器的行為，完全不打正式站。

    python3 test_v3_mock.py        # 跑全部
    python3 test_v3_mock.py -v     # 印出每個測試名稱

原本這支只是把結果印出來給人看，人不盯著就等於沒測到；改成 unittest 後
壞掉會直接讓 exit code 非 0。
"""

import gzip
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest

import pawchive_client_v3 as pc
import pawchive_download as pdl

HITS = {}
FILE_DATA = bytes(i % 251 for i in range(5000))


# ---------- mock API ----------

class MockAPI(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _hit(self, key):
        HITS[key] = HITS.get(key, 0) + 1
        return HITS[key]

    def _reply(self, code, body=b"", ctype=None, extra=()):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def route(self, method):
        p = self.path.split("?")[0]

        # flag：兩種結果 body 都是空的，靠狀態碼分辨
        if p.endswith("/flag"):
            return self._reply(200 if method == "GET" else 201)

        # 未認證的 favorites 會被導去登入頁（規格明載）
        if p.startswith("/favorites/"):
            return self._reply(302, extra=[("Location", "/account/login")])
        if p.startswith("/account/login"):
            return self._reply(200, b"<!doctype html><html>login</html>", "text/html")

        # 前兩次 503，第三次成功
        if p.startswith("/flaky"):
            if self._hit("flaky") < 3:
                return self._reply(503)
            return self._reply(200, json.dumps({"ok": True}).encode(), "application/json")

        # POST 遇 500：預設不該重試
        if p.startswith("/writeflaky"):
            if self._hit("writeflaky") < 2:
                return self._reply(500)
            return self._reply(201)

        if p.startswith("/gzipped"):
            body = gzip.compress(json.dumps({"hello": "世界"}).encode())
            return self._reply(200, body, "application/json",
                               [("Content-Encoding", "gzip")])

        if p.startswith("/notjson"):
            return self._reply(200, b"not json at all", "text/plain")

        if p.startswith("/emptybody"):
            return self._reply(200)

        return self._reply(404, b'{"error":"nope"}', "application/json")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


# ---------- mock 檔案 CDN ----------

class MockCDN(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        rng = self.headers.get("Range")
        start, end = 0, len(FILE_DATA) - 1
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split("-")
            start = int(spec[0] or 0)
            if len(spec) > 1 and spec[1]:
                end = int(spec[1])

        # 帶了 Range 也照樣回整份 200 —— 真實世界有這種伺服器，
        # 客戶端如果無腦 append 就會把檔案接壞
        if p.startswith("/norange"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(FILE_DATA)))
            self.end_headers()
            return self.wfile.write(FILE_DATA)

        # 宣稱總長 5000 卻只吐 100 bytes（模擬中途斷線）
        if p.startswith("/short"):
            body = FILE_DATA[start:start + 100]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{start + len(body) - 1}/{len(FILE_DATA)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if p.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = FILE_DATA[start:end + 1]
        if rng:
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(FILE_DATA)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_servers = []
CDN = ""


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _servers.append(srv)
    return f"http://127.0.0.1:{srv.server_address[1]}"


def setUpModule():
    global CDN
    pc.BASE = _serve(MockAPI)
    CDN = _serve(MockCDN)


def tearDownModule():
    for srv in _servers:
        srv.shutdown()
        srv.server_close()


# ---------- 底層請求 ----------

class TestRequest(unittest.TestCase):
    def test_flag_200_empty_body_means_flagged(self):
        self.assertIs(pc.is_flagged("fanbox", "1", "2"), True)

    def test_flag_post_returns_true(self):
        self.assertIs(pc.flag_post("fanbox", "1", "2"), True)

    def test_expired_session_redirect_becomes_autherror(self):
        for call in (lambda: pc.favorite_post("bad", "fanbox", "1", "2"),
                     lambda: pc.unfavorite_post("bad", "fanbox", "1", "2"),
                     lambda: pc.favorite_creator("bad", "fanbox", "1"),
                     lambda: pc.unfavorite_creator("bad", "fanbox", "1")):
            with self.assertRaises(pc.AuthError):
                call()

    def test_retries_until_success_on_503(self):
        HITS.pop("flaky", None)
        self.assertEqual(pc.get("/flaky"), {"ok": True})
        self.assertEqual(HITS["flaky"], 3)

    def test_post_is_not_retried_by_default(self):
        # 非冪等：flag 之類的寫入重送會變成 409，寧可失敗也不要重試
        HITS.pop("writeflaky", None)
        with self.assertRaises(pc.PawchiveError):
            pc.request("POST", "/writeflaky", empty_ok=True)
        self.assertEqual(HITS["writeflaky"], 1)

    def test_post_retries_when_explicitly_allowed(self):
        HITS.pop("writeflaky", None)
        pc.request("POST", "/writeflaky", empty_ok=True, retry_on_write=True)
        self.assertEqual(HITS["writeflaky"], 2)

    def test_gzip_response_is_decompressed(self):
        self.assertEqual(pc.get("/gzipped"), {"hello": "世界"})

    def test_non_json_becomes_pawchive_error(self):
        with self.assertRaises(pc.PawchiveError):
            pc.get("/notjson")

    def test_raw_mode_returns_text(self):
        self.assertEqual(pc.get("/notjson", raw=True), "not json at all")

    def test_empty_body_rejected_unless_allowed(self):
        with self.assertRaises(pc.PawchiveError):
            pc.get("/emptybody")
        self.assertIsNone(pc.request("GET", "/emptybody", empty_ok=True))

    def test_404_becomes_notfounderror(self):
        with self.assertRaises(pc.NotFoundError):
            pc.get("/nothing/here")

    def test_retry_after_header_is_respected_and_capped(self):
        self.assertEqual(pc._retry_delay(0, {"Retry-After": "5"}), 5.0)
        self.assertEqual(pc._retry_delay(0, {"Retry-After": "99999"}), pc._MAX_RETRY_AFTER)
        self.assertEqual(pc._retry_delay(0, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 1)
        self.assertEqual(pc._retry_delay(2), 4)

    def test_offset_must_be_multiple_of_page(self):
        for bad in (1, 49, -50, "50", 51):
            with self.assertRaises(ValueError):
                pc._check_offset(bad)
        pc._check_offset(0)
        pc._check_offset(150)


# ---------- 分頁 ----------

class TestPaginate(unittest.TestCase):
    def _pages(self, sizes):
        """依序回傳指定筆數的假頁面，並記錄被要求過的 offset。"""
        seen = []

        def fetch(offset):
            seen.append(offset)
            idx = offset // pc.PAGE
            n = sizes[idx] if idx < len(sizes) else 0
            return [{"i": offset + k} for k in range(n)]

        return fetch, seen

    def test_stops_on_short_page(self):
        fetch, seen = self._pages([50, 50, 3])
        rows = pc.paginate(fetch, max_items=500, delay=0)
        self.assertEqual(len(rows), 103)
        self.assertEqual(seen, [0, 50, 100])

    def test_does_not_fetch_beyond_max_items(self):
        fetch, seen = self._pages([50, 50, 50])
        rows = pc.paginate(fetch, max_items=60, delay=0)
        self.assertEqual(len(rows), 60)
        self.assertEqual(seen, [0, 50])

    def test_offset_cap_stops_paging(self):
        fetch, seen = self._pages([50] * 10)
        pc.paginate(fetch, max_items=1000, delay=0, offset_cap=100)
        self.assertEqual(seen, [0, 50, 100])

    def test_non_list_payload_is_rejected(self):
        with self.assertRaises(pc.PawchiveError):
            pc.paginate(lambda o: {"error": "boom"}, max_items=10, delay=0)

    def test_start_offset_is_validated(self):
        with self.assertRaises(ValueError):
            pc.paginate(lambda o: [], start=17)


# ---------- 檔案清單 ----------

class TestPostFiles(unittest.TestCase):
    POST = {
        "id": "9001",
        "file": {"name": "cover.jpeg", "path": "/aa/bb/cover.jpeg"},
        "attachments": [
            {"name": "cover.jpeg", "path": "/aa/bb/cover.jpeg"},   # 與封面同一個檔
            {"name": "pack.zip", "path": "/cc/dd/pack.zip"},
            {"name": "", "path": "/ee/ff/anon.png"},               # 沒有檔名
        ],
    }

    def test_duplicate_paths_collapse(self):
        names = [f["name"] for f in pc.post_files(self.POST)]
        self.assertEqual(names, ["cover.jpeg", "pack.zip", "9001_2.png"])

    def test_dedupe_can_be_disabled(self):
        self.assertEqual(len(pc.post_files(self.POST, dedupe=False)), 4)

    def test_include_cover_false_skips_cover_entry(self):
        files = pc.post_files(self.POST, include_cover=False)
        self.assertEqual([f["path"] for f in files],
                         ["/aa/bb/cover.jpeg", "/cc/dd/pack.zip", "/ee/ff/anon.png"])

    def test_missing_name_falls_back_with_extension(self):
        anon = pc.post_files(self.POST)[-1]
        self.assertEqual(anon["name"], "9001_2.png")

    def test_urls_point_at_the_right_cdns(self):
        f = pc.post_files(self.POST)[0]
        self.assertTrue(f["url"].startswith(pc.FILE_CDN + "/data/aa/bb/cover.jpeg"))
        self.assertEqual(f["thumb"], pc.THUMB_CDN + "/thumbnail/data/aa/bb/cover.jpeg")

    def test_post_file_urls_is_a_thin_wrapper(self):
        self.assertEqual(pc.post_file_urls(self.POST),
                         [(f["name"], f["url"]) for f in pc.post_files(self.POST)])


# ---------- 檔名淨化 ----------

class TestSafeName(unittest.TestCase):
    def test_strips_path_separators(self):
        self.assertEqual(pdl.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(pdl.safe_name("a/b/c.txt"), "c.txt")
        self.assertEqual(pdl.safe_name("no\\way.txt"), "no_way.txt")

    def test_strips_control_and_illegal_chars(self):
        self.assertEqual(pdl.safe_name('a<b>c:"d|e?f*g.txt'), "a_b_c__d_e_f_g.txt")
        self.assertEqual(pdl.safe_name("tab\there.txt"), "tab_here.txt")

    def test_falls_back_when_empty(self):
        self.assertEqual(pdl.safe_name("", "fb"), "fb")
        self.assertEqual(pdl.safe_name("...", "fb"), "fb")
        self.assertEqual(pdl.safe_name(None, "fb"), "fb")

    def test_truncates_by_bytes_not_characters(self):
        # 這是重點：檔案系統的 255 上限算的是 bytes，日文檔名一個字 3 bytes
        name = pdl.safe_name("あ" * 200 + ".jpg")
        self.assertLessEqual(len(name.encode("utf-8")), pdl.NAME_MAX_BYTES)
        self.assertTrue(name.endswith(".jpg"), name)
        self.assertNotIn("�", name)          # 不能留下被切一半的亂碼

    def test_short_names_are_untouched(self):
        self.assertEqual(pdl.safe_name("こんにちは.png"), "こんにちは.png")

    def test_long_dotted_name_without_real_extension(self):
        name = pdl.safe_name("あ" * 100 + "。" + "い" * 100)
        self.assertLessEqual(len(name.encode("utf-8")), pdl.NAME_MAX_BYTES)


# ---------- 下載 ----------

class TestDownload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawchive-test-")
        self.dest = os.path.join(self.dir, "f.bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.dest, "rb") as f:
            return f.read()

    def test_remote_size_reads_content_range(self):
        self.assertEqual(pdl.remote_size(f"{CDN}/file"), len(FILE_DATA))

    def test_full_download(self):
        status, size = pdl.download(f"{CDN}/file", self.dest)
        self.assertEqual((status, size), ("done", len(FILE_DATA)))
        self.assertEqual(self.read(), FILE_DATA)

    def test_resume_appends_only_the_missing_part(self):
        with open(self.dest, "wb") as f:
            f.write(FILE_DATA[:2000])
        status, size = pdl.download(f"{CDN}/file", self.dest)
        self.assertEqual(status, "resume")
        self.assertEqual(self.read(), FILE_DATA)

    def test_server_ignoring_range_overwrites_instead_of_appending(self):
        # 迴歸測試：伺服器不理 Range 回了 200 整份，舊版會 append 成
        # 2000+5000=7000 bytes 的壞檔
        with open(self.dest, "wb") as f:
            f.write(FILE_DATA[:2000])
        status, size = pdl.download(f"{CDN}/norange", self.dest)
        self.assertEqual(status, "done")
        self.assertEqual(os.path.getsize(self.dest), len(FILE_DATA))
        self.assertEqual(self.read(), FILE_DATA)

    def test_complete_file_is_skipped(self):
        with open(self.dest, "wb") as f:
            f.write(FILE_DATA)
        self.assertEqual(pdl.download(f"{CDN}/file", self.dest)[0], "skip")

    def test_oversized_local_file_is_redownloaded(self):
        with open(self.dest, "wb") as f:
            f.write(FILE_DATA + b"junk")
        status, _ = pdl.download(f"{CDN}/file", self.dest)
        self.assertEqual(status, "done")
        self.assertEqual(self.read(), FILE_DATA)

    def test_size_limit(self):
        status, size = pdl.download(f"{CDN}/file", self.dest, max_bytes=100)
        self.assertEqual(status, "toobig")
        self.assertFalse(os.path.exists(self.dest))

    def test_truncated_transfer_is_reported_not_silently_kept(self):
        with self.assertRaises(OSError):
            pdl.download(f"{CDN}/short", self.dest, retries=2)

    def test_missing_file_raises_without_retrying(self):
        with self.assertRaises(Exception):
            pdl.download(f"{CDN}/missing", self.dest)


class TestDownloadPostFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawchive-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_same_name_files_do_not_overwrite_each_other(self):
        files = [("dup.bin", f"{CDN}/file"), ("dup.bin", f"{CDN}/file")]
        res = pdl.download_post_files(files, self.dir, "p1", workers=2)
        self.assertEqual(len(res), 2)
        self.assertEqual(sorted(os.listdir(self.dir)), ["dup.bin", "dup_1.bin"])

    def test_results_keep_input_order(self):
        files = [(f"f{i}.bin", f"{CDN}/file") for i in range(6)]
        res = pdl.download_post_files(files, self.dir, "p1", workers=3)
        self.assertEqual([r[0] for r in res], [f[0] for f in files])
        self.assertTrue(all(r[1] == "done" for r in res), res)

    def test_failures_are_reported_not_raised(self):
        files = [("ok.bin", f"{CDN}/file"), ("bad.bin", f"{CDN}/missing")]
        res = pdl.download_post_files(files, self.dir, "p1", workers=2)
        self.assertIsNone(res[0][3])
        self.assertIsNotNone(res[1][3])

    def test_should_stop_halts_remaining_files(self):
        files = [(f"f{i}.bin", f"{CDN}/file") for i in range(5)]
        res = pdl.download_post_files(files, self.dir, "p1", workers=1,
                                      should_stop=lambda: True)
        self.assertEqual(res, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
