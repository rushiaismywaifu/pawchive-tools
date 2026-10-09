#!/usr/bin/env python3
"""
Pawchive 圖形化瀏覽器 — 本機 Web GUI

啟動後會自動開啟瀏覽器，可以用滑鼠瀏覽、搜尋、預覽、下載。

    python3 pawchive_gui.py                # 預設 http://127.0.0.1:8765
    python3 pawchive_gui.py --port 9000    # 換連接埠
    python3 pawchive_gui.py --no-browser   # 不自動開瀏覽器
    python3 pawchive_gui.py --out ~/pics   # 指定下載輸出目錄

為什麼需要這個本機伺服器？
    Pawchive 的 API **沒有回傳 CORS 標頭**，瀏覽器裡的 JS 無法直接呼叫，
    所以由這支程式在本機代理 API 請求。
    圖片 CDN 則有 `access-control-allow-origin: *`，前端直連即可，不經過代理，
    這樣縮圖載入才會快。

安全性：
    這個伺服器沒有帳號密碼，只要能連上就能操作。因此預設只綁 127.0.0.1，
    並檢查 Host / Origin，擋掉其他網頁對本機發的請求（CSRF）與 DNS rebinding。
    下載目錄由 --out 決定，不接受請求端指定。

只依賴標準函式庫 + 同目錄的 pawchive_client_v3.py。
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pawchive_client_v3 as pc
import pawchive_download as pdl

DOWNLOAD_ROOT = os.path.join(os.getcwd(), "downloads")   # 由 main() 依 --out 覆寫
DOWNLOAD_WORKERS = 3

# ---------- 創作者清單快取（12MB / 9 萬筆，只抓一次） ----------

_creators = None
_creators_index = None       # [(小寫檔名, creator), ...]，依 favorited 遞減排序，算一次就好
_creators_lock = threading.Lock()
_creators_err = None
_creators_err_at = 0.0
_CREATORS_ERR_TTL = 30   # 失敗只記 30 秒；網路抽風一次不該要求使用者重開程式


def creators_cached():
    global _creators, _creators_err, _creators_err_at
    with _creators_lock:
        if _creators is not None:
            return _creators
        if _creators_err and time.time() - _creators_err_at < _CREATORS_ERR_TTL:
            raise RuntimeError(_creators_err)
        try:
            _creators = pc.all_creators()
        except Exception as e:
            _creators_err = f"{type(e).__name__}: {e}"
            _creators_err_at = time.time()
            raise RuntimeError(_creators_err)
        _creators_err = None
        return _creators


def creators_index():
    """
    建一次「小寫檔名 + 依收藏數排序」的索引，之後的搜尋與預設清單都複用它。

    好處：
      - 不用每次請求都對 9 萬個名字重新 lower()（搜尋框每敲一次就是 9 萬次）
      - index 已依收藏數遞減排好，搜尋只要從前端收集到 limit 筆就能停，
        常見的熱門關鍵字不用掃完整份清單
    """
    global _creators_index
    data = creators_cached()
    with _creators_lock:
        if _creators_index is None:
            _creators_index = sorted(
                (((c.get("name") or "").lower(), c) for c in data),
                key=lambda nc: nc[1].get("favorited") or 0, reverse=True)
        return _creators_index


def creators_top(n=100):
    """沒有關鍵字時的預設清單（收藏數最高的前 n 位）。"""
    return [c for _, c in creators_index()[:n]]


def filter_creators(index, term="", svc="", limit=200):
    """
    在「已排序」的索引裡篩選，保留 favorited 順序並在湊滿 limit 時提早結束。

    index 的每項是 (小寫檔名, creator)；term 需為已轉小寫的字串。
    因為回傳本來就是收藏數前幾名，從已排序索引依序取前 limit 筆，
    結果等同「全部篩選再排序取前 limit」，但常見關鍵字會早停。
    """
    out = []
    for low, c in index:
        if term and term not in low:
            continue
        if svc and c.get("service") != svc:
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


# ---------- 下載工作管理 ----------

JOBS = {}
_jobs_lock = threading.Lock()      # 工作執行緒在寫、HTTP 執行緒在讀，兩邊都要走這把鎖
_job_seq = 0
MAX_JOBS = 20                      # 只留最近幾筆，開整天不會無限長大
JOB_LOG_LINES = 300


def _job_view(job, tail=40):
    """
    複製一份給 HTTP 執行緒序列化。

    直接把 job dict 丟給 json.dumps 會在工作執行緒同時 append / 截斷 log 時
    讀到不一致的狀態甚至拋例外，所以一律先在鎖裡複製。
    順便只回最後幾行 log——前端也只顯示 8 行，沒必要每秒傳 300 行過去。
    """
    with _jobs_lock:
        view = dict(job)
        view["log"] = job["log"][-tail:]
        return view


def start_job(service, cid, cname, max_posts, metadata_only, max_mb):
    global _job_seq
    with _jobs_lock:
        _job_seq += 1
        jid = str(_job_seq)
        job = {"id": jid, "state": "running", "log": [], "done": 0, "total": 0,
               "creator": cname or cid, "bytes": 0, "failed": 0}
        JOBS[jid] = job
        for k in sorted(JOBS, key=int):      # 淘汰最舊的已結束工作
            if len(JOBS) <= MAX_JOBS:
                break
            if JOBS[k]["state"] != "running":
                del JOBS[k]

    def log(msg):
        with _jobs_lock:
            job["log"].append(msg)
            del job["log"][:-JOB_LOG_LINES]

    def bump(key, n):
        with _jobs_lock:
            job[key] += n

    def setf(key, val):
        # 所有 job 欄位寫入都走鎖，和 _job_view 的讀取、/api/cancel 的寫入對稱，
        # 避免 HTTP 執行緒序列化當下讀到半套狀態（free-threaded Python 下尤其重要）。
        with _jobs_lock:
            job[key] = val

    def cancelled():
        with _jobs_lock:
            return job["state"] == "cancelled"

    def run():
        try:
            max_bytes = int(max_mb * 1024 * 1024) if max_mb else None
            posts = pc.paginate(
                lambda o: pc.creator_posts(service, cid, o),
                max_items=max_posts, delay=0.5,
            )
            setf("total", len(posts))
            log(f"取得 {len(posts)} 篇貼文")
            root = os.path.join(DOWNLOAD_ROOT, f"{service}_{pdl.safe_name(cname, cid)}_{cid}")
            for i, post in enumerate(posts, 1):
                if cancelled():
                    log("已取消"); break
                pid = post.get("id")
                date = (post.get("published") or "")[:10]
                title = (post.get("title") or "")[:40]
                pdir = os.path.join(root, pdl.safe_name(f"{date}_{pid}_{title}", pid))
                os.makedirs(pdir, exist_ok=True)
                with open(os.path.join(pdir, "post.json"), "w", encoding="utf-8") as f:
                    json.dump(post, f, ensure_ascii=False, indent=2)
                log(f"[{i}/{len(posts)}] {date} {title}")
                if not metadata_only:
                    for name, status, size, err in pdl.download_post_files(
                            pc.post_file_urls(post), pdir, pid, max_bytes,
                            DOWNLOAD_WORKERS, cancelled):
                        if err:
                            bump("failed", 1)
                            log(f"    ✗ {name} 失敗: {err.split(':')[0]}")
                            continue
                        if status in ("done", "resume"):
                            bump("bytes", size)
                        mark = {"done": "✓", "resume": "↻", "skip": "·", "toobig": "✗"}[status]
                        log(f"    {mark} {name} ({pdl.human(size)})")
                setf("done", i)
            if not cancelled():
                setf("state", "done")
                log(f"完成，共下載 {pdl.human(job['bytes'])}"
                    + (f"，{job['failed']} 個失敗" if job["failed"] else ""))
        except Exception as e:
            setf("state", "error")
            log(f"錯誤: {type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return jid


# ---------- HTTP handler ----------

# 綁在 loopback 時允許的 Host 值；綁 0.0.0.0 之類的位址時清空（見 main()）
ALLOWED_HOSTS = set()
MAX_BODY = 64 * 1024

# 貼文內文是第三方 HTML，前端雖然有清洗，仍再上一層瀏覽器強制的防線：
# 只准載入兩個圖片 CDN，禁止任何外部腳本、連線與表單送出。
CSP = ("default-src 'none'; "
       "img-src https://img.pawchive.pw https://file.pawchive.pw data:; "
       "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; form-action 'none'; base-uri 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "PawchiveGUI"
    protocol_version = "HTTP/1.1"   # 開 keep-alive，前端連打數十個 API 不用每次重連
    timeout = 30                    # 閒置的 keep-alive 連線要收掉，否則執行緒一直卡著

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _allowed(self):
        """
        擋掉別的網頁對這台本機伺服器下指令。

        這個 server 沒有認證，任何使用者開著的分頁都能對 127.0.0.1:8765 送
        POST /api/download（CSRF），或把自家網域 DNS 指到 127.0.0.1 再讀 API
        （DNS rebinding）。兩道檢查：
          - Host 必須是我們自己綁的位址（rebinding 送過來的是攻擊者的網域）
          - 有帶 Origin 就必須與 Host 同源（跨站請求一定帶 Origin）
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower().rstrip("/")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            return False
        return True

    @staticmethod
    def _arg(q, key, required=True):
        v = (q.get(key) or [""])[0].strip()
        if required and not v:
            raise ValueError(f"缺少必要參數：{key}")
        if "/" in v or "\\" in v:
            raise ValueError(f"參數 {key} 含有非法字元")
        return v or None

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden: cross-origin request"})

        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]
        arg = lambda k, required=True: self._arg(q, k, required)
        p = u.path

        try:
            if p == "/" or p == "/index.html":
                return self._send(200, HTML, "text/html; charset=utf-8")

            if p == "/api/recent":
                off = int(one("o", "0") or 0)
                return self._send(200, pc.recent_posts(off, q=one("q")))

            if p == "/api/creator":
                return self._send(200, pc.creator_profile(arg("service"), arg("id")))

            if p == "/api/creator_posts":
                off = int(one("o", "0") or 0)
                return self._send(200, pc.creator_posts(
                    arg("service"), arg("id"), off, q=one("q")))

            if p == "/api/post":
                svc, cid, pid = arg("service"), arg("id"), arg("post")
                post = pc.single_post(svc, cid, pid)
                # 每筆都帶 thumb：前端預覽用縮圖（原檔約 5 倍大），點下去才開原檔
                post["_files"] = pc.post_files(post)
                try:
                    post["_comments"] = pc.post_comments(svc, cid, pid)
                except Exception:
                    post["_comments"] = []
                return self._send(200, post)

            if p == "/api/search_creators":
                term = (one("q") or "").strip().lower()
                svc = one("service") or ""
                if not term and not svc:
                    return self._send(200, creators_top(100))
                rows = filter_creators(creators_index(), term, svc, 200)
                return self._send(200, rows)

            if p == "/api/jobs":
                with _jobs_lock:
                    jobs = list(JOBS.values())
                return self._send(200, [_job_view(j) for j in jobs])

            if p == "/api/job":
                with _jobs_lock:
                    j = JOBS.get(one("id"))
                return self._send(200, _job_view(j)) if j else \
                    self._send(404, {"error": "no such job"})

            return self._send(404, {"error": "not found"})

        except pc.NotFoundError as e:
            return self._send(404, {"error": str(e)})
        except pc.AuthError as e:
            return self._send(401, {"error": str(e)})
        except (pc.PawchiveError, RuntimeError) as e:
            return self._send(502, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden: cross-origin request"})

        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            # 不能只讀前面一段就算了：開著 keep-alive 時，沒讀完的 body
            # 會被當成下一個請求解析，整條連線就錯位了。直接關掉最乾淨。
            self.close_connection = True
            return self._send(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        try:
            if u.path == "/api/download":
                svc = str(body.get("service") or "").strip()
                cid = str(body.get("id") or "").strip()
                if not svc or not cid:
                    return self._send(400, {"error": "缺少 service 或 id"})
                # 輸出位置由啟動參數 --out 決定，不接受請求指定，
                # 否則任何送得進來的請求都能挑選寫入路徑。
                jid = start_job(
                    svc, cid, body.get("name"),
                    max(1, int(body.get("max") or 20)),
                    bool(body.get("metadata_only")),
                    max(0.0, float(body.get("max_mb") or 0)),
                )
                return self._send(200, {"job": jid})

            if u.path == "/api/cancel":
                with _jobs_lock:
                    j = JOBS.get(str(body.get("id") or ""))
                    if j and j["state"] == "running":
                        j["state"] = "cancelled"
                return self._send(200, {"ok": True})

            if u.path == "/api/forget":
                with _jobs_lock:
                    j = JOBS.get(str(body.get("id") or ""))
                    if j and j["state"] != "running":
                        del JOBS[j["id"]]
                return self._send(200, {"ok": True})

            return self._send(404, {"error": "not found"})
        except (TypeError, ValueError) as e:
            return self._send(400, {"error": f"參數格式錯誤：{e}"})


# ---------- 前端 ----------

HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pawchive 瀏覽器</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --tx:#e6e8ee; --dim:#9aa2b1; --acc:#5b8cff; --ok:#3ecf8e; --warn:#ffb020; --err:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,"Noto Sans TC","Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--tx)}
  header{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
         padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .brand{font-weight:700;letter-spacing:.3px;margin-right:4px}
  .brand small{color:var(--dim);font-weight:400;margin-left:6px}
  input,select,button{font:inherit;color:var(--tx);background:var(--panel2);
        border:1px solid var(--line);border-radius:8px;padding:7px 10px;outline:none}
  input:focus,select:focus{border-color:var(--acc)}
  button{cursor:pointer;transition:.15s}
  button:hover{border-color:var(--acc)}
  button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
  button.primary:hover{filter:brightness(1.1)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .tabs{display:flex;gap:6px}
  .tab{padding:7px 14px;border-radius:8px;background:transparent;border:1px solid transparent;color:var(--dim)}
  .tab.on{background:var(--panel2);border-color:var(--line);color:var(--tx);font-weight:600}
  .grow{flex:1}
  main{padding:14px;max-width:1500px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;
        cursor:pointer;transition:.15s;display:flex;flex-direction:column}
  .card:hover{transform:translateY(-2px);border-color:var(--acc)}
  .thumb{aspect-ratio:1;background:var(--panel2);position:relative;overflow:hidden;
         display:flex;align-items:center;justify-content:center}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .thumb .ph{color:var(--dim);font-size:12px}
  .blur .thumb img{filter:blur(18px)}
  .badge{position:absolute;right:6px;bottom:6px;background:#000a;border-radius:6px;
         padding:2px 7px;font-size:11px;color:#fff}
  .meta{padding:8px 10px}
  .meta .t{font-size:13px;font-weight:600;line-height:1.35;
           display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .meta .s{color:var(--dim);font-size:11px;margin-top:4px;display:flex;justify-content:space-between;gap:6px}
  .rows{display:flex;flex-direction:column;gap:8px}
  .row{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
       display:flex;align-items:center;gap:12px;cursor:pointer}
  .row:hover{border-color:var(--acc)}
  .row .nm{font-weight:600}
  .pill{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--panel2);
        border:1px solid var(--line);color:var(--dim)}
  .pill.fanbox{color:#7ec4ff;border-color:#2b4b6b}
  .pill.patreon{color:#ff8a7a;border-color:#6b3630}
  .muted{color:var(--dim)}
  .center{text-align:center;padding:44px 10px;color:var(--dim)}
  .err{background:#3a1d1d;border:1px solid #6b3030;color:#ffc9c9;
       padding:10px 12px;border-radius:10px;margin:10px 0;white-space:pre-wrap}
  /* modal */
  .mask{position:fixed;inset:0;background:#000c;display:none;z-index:50;overflow:auto;padding:24px}
  .mask.on{display:block}
  .modal{max-width:1000px;margin:0 auto;background:var(--panel);border:1px solid var(--line);
         border-radius:14px;overflow:hidden}
  .mhead{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:flex-start}
  .mbody{padding:16px;max-height:none}
  .mbody .content{background:var(--panel2);border-radius:10px;padding:12px;margin:10px 0;
                  word-break:break-word;line-height:1.7}
  .mbody .content img{max-width:100%;border-radius:8px}
  .mbody .content a{color:var(--acc)}
  .files{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-top:8px}
  .file{background:var(--panel2);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .file img{width:100%;display:block;background:#0b0d11;cursor:zoom-in}
  .blur .file img{filter:blur(22px)}
  .file .fn{padding:7px 9px;font-size:11px;color:var(--dim);display:flex;
            justify-content:space-between;gap:8px;align-items:center}
  .file .fn a{color:var(--acc);text-decoration:none;white-space:nowrap}
  .x{margin-left:auto;background:transparent;border:none;color:var(--dim);font-size:22px;
     line-height:1;padding:0 4px;cursor:pointer}
  .x:hover{color:var(--tx)}
  /* jobs */
  #jobs{position:fixed;right:14px;bottom:14px;width:390px;max-height:56vh;overflow:auto;z-index:40;
        display:flex;flex-direction:column;gap:8px}
  .job{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px;
       box-shadow:0 8px 28px #0008}
  .job .jh{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px}
  .bar{height:5px;background:var(--panel2);border-radius:99px;overflow:hidden;margin:8px 0}
  .bar>i{display:block;height:100%;background:var(--acc);width:0;transition:.3s}
  .job pre{margin:0;font-size:11px;line-height:1.5;color:var(--dim);max-height:130px;
           overflow:auto;white-space:pre-wrap}
  .dot{width:8px;height:8px;border-radius:99px;background:var(--acc)}
  .dot.done{background:var(--ok)} .dot.error{background:var(--err)} .dot.cancelled{background:var(--warn)}
  .more{display:block;margin:16px auto;padding:9px 26px}
  label.ck{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px;cursor:pointer}
  .hint{font-size:12px;color:var(--dim);margin:2px 0 12px}
</style>
</head>
<body>
<header>
  <div class="brand">Pawchive <small>本機瀏覽器</small></div>
  <div class="tabs">
    <button class="tab on" data-v="recent">最新貼文</button>
    <button class="tab" data-v="creators">找創作者</button>
  </div>
  <input id="q" class="grow" placeholder="搜尋…（最新貼文＝搜全站貼文；找創作者＝搜名稱）" style="min-width:220px">
  <select id="svc">
    <option value="">全部服務</option>
    <option value="fanbox">fanbox</option>
    <option value="patreon">patreon</option>
  </select>
  <button class="primary" id="go">搜尋</button>
  <label class="ck" title="打勾時所有縮圖與圖片會被模糊處理，方便在公共場合瀏覽。設定會被記住。">
    <input type="checkbox" id="blur" checked> 模糊預覽</label>
</header>

<main>
  <div id="err"></div>
  <div id="view"></div>
</main>

<div class="mask" id="mask"><div class="modal" id="modal"></div></div>
<div id="jobs"></div>

<script>
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if(c) e.className = c;
                          if(h !== undefined) e.innerHTML = h; return e; };
const esc = s => (s??'').toString().replace(/[&<>"']/g, m =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const FILE_CDN = 'https://file.pawchive.pw';
const THUMB_CDN = 'https://img.pawchive.pw';
const URL_ATTRS = ['href','src','srcset','action','formaction','xlink:href','data','poster'];
// 貼文內文是第三方 HTML，直接塞進 DOM 有 XSS 風險。
// 這裡刻意保留排版標籤（要能正常閱讀），只剝掉可執行的部分。
function sanitize(html){
  const d = new DOMParser().parseFromString(html, 'text/html');
  // base 也要拿掉：留著能把頁面上所有相對網址改指到別的網站
  d.querySelectorAll('script,style,iframe,object,embed,form,link,meta,base').forEach(n=>n.remove());
  d.querySelectorAll('*').forEach(n=>{
    [...n.attributes].forEach(a=>{
      const k = a.name.toLowerCase(), v = (a.value||'').replace(/\s+/g,'').toLowerCase();
      if(k.startsWith('on')) n.removeAttribute(a.name);
      else if(URL_ATTRS.includes(k) &&
              (v.startsWith('javascript:') || v.startsWith('vbscript:') || v.startsWith('data:text/html')))
        n.removeAttribute(a.name);
    });
    if(n.tagName === 'A'){ n.setAttribute('target','_blank'); n.setAttribute('rel','noopener noreferrer'); }
  });
  return d.body.innerHTML;
}
const fileUrl  = p => FILE_CDN + '/data' + p;
const thumbUrl = p => THUMB_CDN + '/thumbnail/data' + p;
// 縮圖優先、載不到再退回原始檔：img CDN 偶爾 502，但正常時省很多流量
// （實測同一張圖 34KB / 800px 對 167KB / 1200px），一頁 50 張差距很明顯。
function previewImg(thumb, full, onDead){
  const im = el('img'); im.loading = 'lazy'; im.src = thumb;
  im.onerror = () => {
    if(im.dataset.fell){ if(onDead) onDead(im); return; }
    im.dataset.fell = '1'; im.src = full;
  };
  return im;
}

let view = 'recent', offset = 0, ctx = null, busy = false;

function setErr(m){ $('#err').innerHTML = m ? `<div class="err">${esc(m)}</div>` : ''; }
// 模糊預覽：預設開啟（避免在公共場合尷尬），但會記住使用者的選擇。
// 想改成預設關閉，把下面的 '1' 改成 '0' 即可。
const BLUR_DEFAULT = '1';
function applyBlur(){
  const on = $('#blur').checked;
  document.body.classList.toggle('blur', on);
  try{ localStorage.setItem('pawchive_blur', on ? '1' : '0'); }catch(e){}
}
try{
  $('#blur').checked = (localStorage.getItem('pawchive_blur') ?? BLUR_DEFAULT) === '1';
}catch(e){}
$('#blur').onchange = applyBlur;
document.body.classList.toggle('blur', $('#blur').checked);

async function api(path){
  const r = await fetch(path);
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { throw new Error(t.slice(0,300)); }
  if(!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

/* ---------- 卡片 ---------- */
function postCard(p){
  const c = el('div','card');
  const th = el('div','thumb');
  const path = p.file && p.file.path;
  if(path){
    th.appendChild(previewImg(thumbUrl(path), fileUrl(path),
      () => { th.innerHTML = '<span class="ph">圖片載入失敗</span>'; }));
  } else th.innerHTML = '<span class="ph">無預覽圖</span>';
  const n = (p.attachments || []).length;
  if(n) th.appendChild(el('span','badge', n + ' 個附件'));
  c.appendChild(th);
  c.appendChild(el('div','meta',
    `<div class="t">${esc(p.title || '(無標題)')}</div>
     <div class="s"><span class="pill ${esc(p.service)}">${esc(p.service)}</span>
     <span>${esc((p.published||'').slice(0,10))}</span></div>`));
  c.onclick = () => openPost(p.service, p.user, p.id);
  return c;
}

function creatorRow(c){
  const r = el('div','row');
  r.innerHTML = `<span class="pill ${esc(c.service)}">${esc(c.service)}</span>
    <span class="nm">${esc(c.name || '(未命名)')}</span>
    <span class="muted">ID ${esc(c.id)}</span>
    <span class="grow"></span>
    <span class="muted">♥ ${c.favorited ?? 0}</span>`;
  r.onclick = () => openCreator(c.service, c.id, c.name);
  return r;
}

/* ---------- 列表 ---------- */
async function load(reset){
  if(busy) return; busy = true;
  if(reset){ offset = 0; $('#view').innerHTML = '<div class="center">載入中…</div>'; setErr(''); }
  try{
    const q = encodeURIComponent($('#q').value.trim());
    if(view === 'creators'){
      const rows = await api(`/api/search_creators?q=${q}&service=${$('#svc').value}`);
      $('#view').innerHTML = '';
      $('#view').appendChild(el('div','hint',
        `找到 ${rows.length} 位創作者${rows.length>=200?'（僅顯示前 200 筆，請縮小關鍵字）':''}　點一下看作品`));
      const box = el('div','rows');
      rows.forEach(c => box.appendChild(creatorRow(c)));
      $('#view').appendChild(rows.length ? box : el('div','center','沒有符合的創作者'));
    } else {
      let rows, head = '';
      if(ctx){
        rows = await api(`/api/creator_posts?service=${ctx.s}&id=${ctx.i}&o=${offset}&q=${q}`);
        head = `${esc(ctx.n || ctx.i)} 的貼文`;
      } else {
        rows = await api(`/api/recent?o=${offset}&q=${q}`);
        head = q ? '搜尋結果' : '全站最新貼文';
      }
      if(reset){
        $('#view').innerHTML = '';
        const bar = el('div','hint');
        bar.innerHTML = head + (ctx ? ' ' : '');
        if(ctx){
          const b = el('button', null, '下載這位創作者…');
          b.style.marginLeft = '8px';
          b.onclick = () => askDownload(ctx.s, ctx.i, ctx.n);
          const b2 = el('button', null, '← 回到最新貼文');
          b2.style.marginLeft = '6px';
          b2.onclick = () => { ctx = null; load(true); };
          bar.appendChild(b); bar.appendChild(b2);
        }
        $('#view').appendChild(bar);
        $('#view').appendChild(el('div','grid'));
      }
      const g = $('#view').querySelector('.grid');
      rows.forEach(p => g.appendChild(postCard(p)));
      const old = $('#view').querySelector('.more'); if(old) old.remove();
      if(rows.length === 0 && offset === 0){
        $('#view').appendChild(el('div','center','沒有結果'));
      } else if(rows.length === 50){
        const b = el('button','more','載入更多');
        b.onclick = () => { offset += 50; b.disabled = true; b.textContent = '載入中…';
                            load(false).then(()=>{}); };
        $('#view').appendChild(b);
      }
    }
  }catch(e){
    setErr('載入失敗：' + e.message);
    if(reset){ $('#view').innerHTML = ''; }
    else {
      // 「載入更多」按下去就被停用了，這裡不還原的話整頁只能重新整理才救得回來。
      offset = Math.max(0, offset - 50);   // 退回失敗的那一頁，讓使用者原地重試
      const b = $('#view').querySelector('.more');
      if(b){ b.disabled = false; b.textContent = '重試載入更多'; }
    }
  }
  busy = false;
}

/* ---------- 貼文詳情 ---------- */
async function openPost(s, u, id){
  $('#mask').classList.add('on');
  $('#modal').innerHTML = '<div class="center">載入中…</div>';
  try{
    const p = await api(`/api/post?service=${s}&id=${u}&post=${id}`);
    const files = p._files || [];
    const cm = p._comments || [];
    const m = el('div');
    const h = el('div','mhead');
    h.innerHTML = `<div><div style="font-size:17px;font-weight:700">${esc(p.title||'(無標題)')}</div>
      <div class="muted" style="font-size:12px;margin-top:4px">
      <span class="pill ${esc(p.service)}">${esc(p.service)}</span>
      發布 ${esc((p.published||'').slice(0,16).replace('T',' '))}
      ${p.edited ? '· 編輯 ' + esc(p.edited.slice(0,10)) : ''}
      · ${files.length} 個檔案</div></div>`;
    const x = el('button','x','×'); x.onclick = closeModal; h.appendChild(x);
    m.appendChild(h);

    const b = el('div','mbody');
    if(p.content && p.content.trim())
      b.appendChild(el('div','content', sanitize(p.content)));
    else
      b.appendChild(el('div','muted','（這篇沒有文字內容）'));

    if(files.length){
      const bar = el('div',null);
      bar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:14px';
      bar.innerHTML = '<b>檔案</b>';
      const copy = el('button', null, '複製全部網址');
      copy.onclick = () => { navigator.clipboard.writeText(files.map(f=>f.url).join('\n'));
                             copy.textContent = '已複製 ✓';
                             setTimeout(()=>copy.textContent='複製全部網址',1500); };
      bar.appendChild(copy);
      b.appendChild(bar);
      const g = el('div','files');
      files.forEach(f => {
        const d = el('div','file');
        if(/\.(jpe?g|png|gif|webp|bmp)$/i.test(f.name)){
          // 預覽格最寬也就 ~500px，用縮圖就夠；要看原圖點一下另開分頁
          const im = previewImg(f.thumb || f.url, f.url, () => im.remove());
          im.onclick = () => window.open(f.url,'_blank');
          d.appendChild(im);
        }
        d.appendChild(el('div','fn',
          `<span style="overflow:hidden;text-overflow:ellipsis">${esc(f.name)}</span>
           <a href="${esc(f.url)}" target="_blank" download>下載</a>`));
        g.appendChild(d);
      });
      b.appendChild(g);
    }

    if(cm.length){
      b.appendChild(el('div',null,`<b style="display:block;margin-top:16px">留言 (${cm.length})</b>`));
      cm.forEach(c => b.appendChild(el('div','content',
        `<b>${esc(c.commenter_name || c.commenter || '匿名')}</b>
         <span class="muted" style="font-size:11px">${esc((c.published||'').slice(0,16).replace('T',' '))}</span>
         <div style="margin-top:4px">${esc(c.content||'')}</div>`)));
    }

    const nav = el('div',null); nav.style.cssText='display:flex;gap:8px;margin-top:16px';
    if(p.prev){ const x2=el('button',null,'← 上一篇');
      x2.onclick=()=>openPost(s,u,p.prev); nav.appendChild(x2); }
    if(p.next){ const x3=el('button',null,'下一篇 →');
      x3.onclick=()=>openPost(s,u,p.next); nav.appendChild(x3); }
    const x4 = el('button',null,'看這位創作者的全部作品');
    x4.onclick = () => { closeModal(); openCreator(s, u, null); };
    nav.appendChild(x4);
    b.appendChild(nav);
    m.appendChild(b);
    $('#modal').innerHTML = ''; $('#modal').appendChild(m);
    $('#mask').scrollTop = 0;
  }catch(e){
    $('#modal').innerHTML = `<div class="mbody"><div class="err">${esc(e.message)}</div></div>`;
  }
}
function closeModal(){ $('#mask').classList.remove('on'); }
$('#mask').onclick = e => { if(e.target === $('#mask')) closeModal(); };
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeModal(); });

async function openCreator(s, id, name){
  closeModal();
  ctx = {s, i:id, n:name};
  view = 'recent';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.v === 'recent'));
  $('#q').value = '';
  if(!name){
    try{ const p = await api(`/api/creator?service=${s}&id=${id}`); ctx.n = p.name; }catch{}
  }
  load(true);
}

/* ---------- 下載 ---------- */
function askDownload(s, id, name){
  const n = prompt(`要下載「${name || id}」的幾篇貼文？\n（輸入數字；打 all 表示全部）`, '20');
  if(n === null) return;
  const max = (n.trim().toLowerCase() === 'all') ? 9999 : parseInt(n, 10);
  if(!max || max < 1){ alert('請輸入正整數'); return; }
  const meta = confirm('要下載圖片檔嗎？\n\n[確定] 下載圖片＋文字\n[取消] 只存文字 (post.json)');
  fetch('/api/download', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({service:s, id, name, max, metadata_only: !meta})
  }).then(r=>r.json())
    .then(d => { if(d && d.error) alert('無法開始下載：' + d.error); pollJobs(); })
    .catch(e => alert('無法開始下載：' + e.message));
}

const dismissed = new Set();   // 關掉的卡片別在下一次輪詢又冒出來

async function pollJobs(){
  try{
    const jobs = await api('/api/jobs');
    const box = $('#jobs'); box.innerHTML = '';
    jobs.slice().reverse().forEach(j => {
      if(dismissed.has(j.id)) return;
      const d = el('div','job');
      const pct = j.total ? Math.round(j.done / j.total * 100) : 0;
      const label = {running:'下載中', done:'完成', error:'錯誤', cancelled:'已取消'}[j.state];
      const head = el('div','jh');
      head.innerHTML = `<span class="dot ${j.state}"></span>
        <span>${esc(j.creator)}</span>
        <span class="muted" style="font-weight:400">${label} ${j.done}/${j.total||'?'}</span>`;
      if(j.state === 'running'){
        const c = el('button', null, '取消');
        c.style.cssText = 'margin-left:auto;padding:2px 10px;font-size:12px';
        c.onclick = () => fetch('/api/cancel',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({id:j.id})});
        head.appendChild(c);
      } else {
        const c = el('button', null, '×');
        c.style.cssText = 'margin-left:auto;padding:2px 10px;font-size:12px';
        c.onclick = () => { dismissed.add(j.id); d.remove();
          fetch('/api/forget',{method:'POST',headers:{'Content-Type':'application/json'},
                               body:JSON.stringify({id:j.id})}); };
        head.appendChild(c);
      }
      d.appendChild(head);
      const bar = el('div','bar'); bar.innerHTML = `<i style="width:${pct}%"></i>`;
      d.appendChild(bar);
      d.appendChild(el('pre', null, esc((j.log||[]).slice(-8).join('\n'))));
      box.appendChild(d);
    });
    if(jobs.some(j => j.state === 'running')) setTimeout(pollJobs, 1000);
  }catch(e){}
}

/* ---------- 事件 ---------- */
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  t.classList.add('on'); view = t.dataset.v; ctx = null; load(true);
});
$('#go').onclick = () => load(true);
$('#q').addEventListener('keydown', e => { if(e.key === 'Enter') load(true); });
$('#svc').onchange = () => { if(view === 'creators') load(true); };
load(true); pollJobs();
</script>
</body>
</html>
"""


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main():
    global DOWNLOAD_ROOT, DOWNLOAD_WORKERS

    ap = argparse.ArgumentParser(description="Pawchive 圖形化瀏覽器")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "downloads"),
                    help="下載輸出目錄（預設 ./downloads）")
    ap.add_argument("--workers", type=int, default=3,
                    help="同一篇貼文內同時下載幾個檔案（預設 3）")
    a = ap.parse_args()

    DOWNLOAD_ROOT = os.path.abspath(a.out)
    DOWNLOAD_WORKERS = max(1, a.workers)

    # 綁在 loopback 時鎖定 Host，擋 DNS rebinding；綁對外位址時使用者可能用
    # 任何一個 LAN IP 連進來，沒辦法預先列舉，就只留 Origin 同源檢查。
    if a.host in _LOOPBACK:
        for h in _LOOPBACK:
            ALLOWED_HOSTS.add(f"{h}:{a.port}")
            ALLOWED_HOSTS.add(h)
        ALLOWED_HOSTS.add(f"[::1]:{a.port}")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}/"
    print(f"Pawchive GUI 已啟動 → {url}")
    print(f"下載輸出目錄：{DOWNLOAD_ROOT}")
    if a.host not in _LOOPBACK:
        print("提醒：綁在非本機位址，同網段的人都能操作這個介面（含下載）")
    print("按 Ctrl+C 結束\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
