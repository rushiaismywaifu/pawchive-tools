#!/usr/bin/env python3
"""
Pawchive 圖形化瀏覽器 — 本機 Web GUI

啟動後會自動開啟瀏覽器，可以用滑鼠瀏覽、搜尋、預覽、下載。

    python3 pawchive_gui.py                # 預設 http://127.0.0.1:8765
    python3 pawchive_gui.py --port 9000    # 換連接埠
    python3 pawchive_gui.py --no-browser   # 不自動開瀏覽器

為什麼需要這個本機伺服器？
    Pawchive 的 API **沒有回傳 CORS 標頭**，瀏覽器裡的 JS 無法直接呼叫，
    所以由這支程式在本機代理 API 請求。
    圖片 CDN 則有 `access-control-allow-origin: *`，前端直連即可，不經過代理，
    這樣縮圖載入才會快。

只依賴標準函式庫 + 同目錄的 pawchive_client_v3.py。
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pawchive_client_v3 as pc
import pawchive_download as pdl

# ---------- 創作者清單快取（12MB / 9 萬筆，只抓一次） ----------

_creators = None
_creators_lock = threading.Lock()
_creators_err = None


def creators_cached():
    global _creators, _creators_err
    with _creators_lock:
        if _creators is None and _creators_err is None:
            try:
                _creators = pc.all_creators()
            except Exception as e:
                _creators_err = f"{type(e).__name__}: {e}"
        if _creators_err:
            raise RuntimeError(_creators_err)
        return _creators


# ---------- 下載工作管理 ----------

JOBS = {}
_job_seq = [0]


def start_job(service, cid, cname, max_posts, out_dir, metadata_only, max_mb):
    _job_seq[0] += 1
    jid = str(_job_seq[0])
    job = {"id": jid, "state": "running", "log": [], "done": 0, "total": 0,
           "creator": cname or cid, "bytes": 0, "failed": 0}
    JOBS[jid] = job

    def log(msg):
        job["log"].append(msg)
        del job["log"][:-300]

    def run():
        try:
            max_bytes = int(max_mb * 1024 * 1024) if max_mb else None
            posts = pc.paginate(
                lambda o: pc.creator_posts(service, cid, o),
                max_items=max_posts, delay=0.5,
            )
            job["total"] = len(posts)
            log(f"取得 {len(posts)} 篇貼文")
            root = os.path.join(out_dir, f"{service}_{pdl.safe_name(cname, cid)}_{cid}")
            for i, post in enumerate(posts, 1):
                if job["state"] == "cancelled":
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
                    for name, url in pc.post_file_urls(post):
                        if job["state"] == "cancelled":
                            break
                        dest = os.path.join(pdir, pdl.safe_name(name, f"{pid}_file"))
                        try:
                            status, size = pdl.download(url, dest, max_bytes)
                            if status in ("done", "resume"):
                                job["bytes"] += size
                            mark = {"done": "✓", "resume": "↻", "skip": "·", "toobig": "✗"}[status]
                            log(f"    {mark} {name} ({pdl.human(size)})")
                        except Exception as e:
                            job["failed"] += 1
                            log(f"    ✗ {name} 失敗: {type(e).__name__}")
                job["done"] = i
            if job["state"] != "cancelled":
                job["state"] = "done"
                log(f"完成，共下載 {pdl.human(job['bytes'])}"
                    + (f"，{job['failed']} 個失敗" if job["failed"] else ""))
        except Exception as e:
            job["state"] = "error"
            log(f"錯誤: {type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return jid


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "PawchiveGUI"

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
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]
        p = u.path

        try:
            if p == "/" or p == "/index.html":
                return self._send(200, HTML, "text/html; charset=utf-8")

            if p == "/api/recent":
                off = int(one("o", "0") or 0)
                return self._send(200, pc.recent_posts(off, q=one("q")))

            if p == "/api/creator":
                svc, cid = one("service"), one("id")
                return self._send(200, pc.creator_profile(svc, cid))

            if p == "/api/creator_posts":
                svc, cid = one("service"), one("id")
                off = int(one("o", "0") or 0)
                return self._send(200, pc.creator_posts(svc, cid, off, q=one("q")))

            if p == "/api/post":
                svc, cid, pid = one("service"), one("id"), one("post")
                post = pc.single_post(svc, cid, pid)
                post["_files"] = [{"name": n, "url": url}
                                  for n, url in pc.post_file_urls(post)]
                try:
                    post["_comments"] = pc.post_comments(svc, cid, pid)
                except Exception:
                    post["_comments"] = []
                return self._send(200, post)

            if p == "/api/search_creators":
                term = (one("q") or "").strip().lower()
                svc = one("service") or ""
                data = creators_cached()
                if not term and not svc:
                    rows = sorted(data, key=lambda c: c.get("favorited") or 0, reverse=True)[:100]
                else:
                    rows = [c for c in data
                            if (not term or term in (c.get("name") or "").lower())
                            and (not svc or c.get("service") == svc)]
                    rows.sort(key=lambda c: c.get("favorited") or 0, reverse=True)
                    rows = rows[:200]
                return self._send(200, rows)

            if p == "/api/jobs":
                return self._send(200, list(JOBS.values()))

            if p == "/api/job":
                j = JOBS.get(one("id"))
                return self._send(200 if j else 404, j or {"error": "no such job"})

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
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if u.path == "/api/download":
            jid = start_job(
                body.get("service"), body.get("id"), body.get("name"),
                int(body.get("max") or 20),
                body.get("out") or os.path.join(os.getcwd(), "downloads"),
                bool(body.get("metadata_only")),
                float(body.get("max_mb") or 0),
            )
            return self._send(200, {"job": jid})

        if u.path == "/api/cancel":
            j = JOBS.get(body.get("id"))
            if j and j["state"] == "running":
                j["state"] = "cancelled"
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


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
// 貼文內文是第三方 HTML，直接塞進 DOM 有 XSS 風險。
// 這裡刻意保留排版標籤（要能正常閱讀），只剝掉可執行的部分。
function sanitize(html){
  const d = new DOMParser().parseFromString(html, 'text/html');
  d.querySelectorAll('script,style,iframe,object,embed,form,link,meta').forEach(n=>n.remove());
  d.querySelectorAll('*').forEach(n=>{
    [...n.attributes].forEach(a=>{
      const k = a.name.toLowerCase(), v = (a.value||'').trim().toLowerCase();
      if(k.startsWith('on')) n.removeAttribute(a.name);
      else if((k==='href'||k==='src') && (v.startsWith('javascript:')||v.startsWith('data:text/html')))
        n.removeAttribute(a.name);
    });
    if(n.tagName === 'A'){ n.setAttribute('target','_blank'); n.setAttribute('rel','noopener noreferrer'); }
  });
  return d.body.innerHTML;
}
const fileUrl = p => FILE_CDN + '/data' + p;

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
    const im = el('img'); im.loading = 'lazy'; im.src = fileUrl(path);
    im.onerror = () => { th.innerHTML = '<span class="ph">圖片載入失敗</span>'; };
    th.appendChild(im);
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
  }catch(e){ setErr('載入失敗：' + e.message); if(reset) $('#view').innerHTML = ''; }
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
          const im = el('img'); im.loading='lazy'; im.src=f.url;
          im.onclick = () => window.open(f.url,'_blank');
          im.onerror = () => { im.remove(); };
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
  }).then(r=>r.json()).then(()=>pollJobs());
}

async function pollJobs(){
  try{
    const jobs = await api('/api/jobs');
    const box = $('#jobs'); box.innerHTML = '';
    jobs.slice().reverse().forEach(j => {
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
        c.onclick = () => { d.remove(); };
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


def main():
    ap = argparse.ArgumentParser(description="Pawchive 圖形化瀏覽器")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}/"
    print(f"Pawchive GUI 已啟動 → {url}")
    print("按 Ctrl+C 結束\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
