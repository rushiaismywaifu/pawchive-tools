#!/usr/bin/env python3
"""
Pawchive API v1 — client v3（純標準函式庫，無需 pip install）

相對 v1 的改進：
  1. request() 支援 GET / POST / DELETE（favorites、flag 需要寫入方法）
  2. 非 JSON 回應（空字串 / 純文字 / HTML）統一包成 PawchiveError，
     不再讓原始 ValueError 洩漏出去（本站錯誤格式很隨性，見 guide 第 6 節）
  3. TimeoutError / 連線重置等 OSError 類錯誤納入指數退避重試
  4. paginate() 的 offset 上限改為參數：/posts 用 50000，創作者貼文傳 None（無上限）
  5. 補完規格全部 19 個操作：favorites 系列、flag、revisions、fancards、app_version…
  6. 打 API 前先驗證 offset 是否為 50 的倍數，避免白白吃 400
  7. post_file_urls() 無檔名時用 {post_id}_{n} 當預設檔名，批次下載不撞名
  8. 自動要求並解壓縮 gzip 回應（/posts 74KB → 31KB，/creators 12MB 更有感）
  9. 429 會照伺服器的 Retry-After 等待（有上限），不再只憑指數退避硬猜
 10. post_files() 依 path 去重：多數貼文的封面與某個 attachment 是同一個檔案，
     不去重等於每篇都重抓一份一樣的圖

session 取得方式：瀏覽器登入 https://pawchive.pw/account/login 後，
從 cookie 複製 session 的值。CLI 可用 --session 參數或環境變數 PAWCHIVE_SESSION。

用法範例:
    python3 pawchive_client_v3.py recent
    python3 pawchive_client_v3.py search "genshin" --max 100 --offset 50
    python3 pawchive_client_v3.py profile fanbox 21971914
    python3 pawchive_client_v3.py posts fanbox 21971914 --max 120   # 自動翻頁（無上限）
    python3 pawchive_client_v3.py post fanbox 21971914 12323115
    python3 pawchive_client_v3.py urls fanbox 21971914 12323115     # 印出下載網址
    python3 pawchive_client_v3.py --session XXX favorites --type post
    python3 pawchive_client_v3.py --session XXX favpost fanbox 21971914 12323115

當函式庫用:

    from pawchive_client_v3 import creator_posts, paginate, post_file_urls

    rows = paginate(lambda o: creator_posts("fanbox", "21971914", o), max_items=200)
    for post in rows:
        for name, url in post_file_urls(post):
            print(name, url)

    # 需要縮圖網址或原始 path 時改用 post_files()，回傳 dict 而非 tuple
    from pawchive_client_v3 import post_files
    for f in post_files(rows[0]):
        print(f["name"], f["url"], f["thumb"])
"""

import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BASE = "https://pawchive.pw/api/v1"
FILE_CDN = "https://file.pawchive.pw"   # 原始檔（主站網域會 404，一定要用 file. 子網域）
THUMB_CDN = "https://img.pawchive.pw"   # 縮圖（偶爾 502，記得 fallback 到原始檔）
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

PAGE = 50                # API 固定每頁 50 筆，offset 必須是 50 的倍數
POSTS_OFFSET_CAP = 50000  # 只有 /posts 有這個硬上限；創作者貼文列表沒有

_RETRY_CODES = {429, 500, 502, 503, 504}
_IDEMPOTENT = {"GET", "HEAD", "DELETE"}   # POST 非冪等，預設不重試（flag 重送會變 409）
_MAX_BACKOFF = 8          # 指數退避上限（秒）
_MAX_RETRY_AFTER = 60     # 伺服器要求的等待秒數再長也不超過這個值，免得整個程式卡死


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自動跟隨轉址：302 在本 API 代表 session 失效，跟隨過去只會拿到登入頁 HTML。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class PawchiveError(Exception):
    """API 呼叫失敗（HTTP 錯誤、連線失敗、回應格式不符預期）。"""


class NotFoundError(PawchiveError):
    """404：資源不存在（創作者 / 貼文 / 檔案 hash 找不到）。"""


class AuthError(PawchiveError):
    """401 / 302：未登入或 session 失效。"""


class ConflictError(PawchiveError):
    """409：狀態衝突（例如貼文已被標記過）。"""


# ---------- 底層請求 ----------

def _decompress(data, encoding):
    """
    urllib 不會自動解壓縮，所以我們主動要 gzip 再自己解。
    實測 /posts 74 KB → 31 KB、/creators 12 MB 更划算，是最便宜的加速手段。
    """
    encoding = (encoding or "").strip().lower()
    if not data or encoding not in ("gzip", "x-gzip", "deflate"):
        return data
    try:
        if encoding == "deflate":
            # 有些伺服器回裸 deflate（不含 zlib 標頭），標準解法失敗就退回裸格式
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        return gzip.decompress(data)
    except (OSError, zlib.error) as e:
        raise PawchiveError(f"回應解壓縮失敗（Content-Encoding: {encoding}）：{e}") from e


def _retry_delay(attempt, headers=None):
    """指數退避；伺服器有給 Retry-After（429 常見）就聽它的，但設上限避免卡死。"""
    if headers:
        ra = headers.get("Retry-After")
        if ra:
            try:
                return max(0.0, min(float(ra.strip()), _MAX_RETRY_AFTER))
            except (ValueError, AttributeError):
                pass  # 也可能是 HTTP-date 格式，解不動就退回指數退避
    return min(2 ** attempt, _MAX_BACKOFF)


def request(method, path, params=None, session=None, raw=False, empty_ok=False,
            retries=3, retry_on_write=False):
    """
    對 API 發請求，回傳解析後的 JSON。

    - method： "GET" / "POST" / "DELETE"
    - session：登入後 cookie 裡的 session 值（只有 favorites 需要）
    - raw=True：不解析 JSON，直接回傳字串（/app_version 回純文字，需要這個）
    - empty_ok=True：2xx 但 body 為空時回 None 而非報錯（寫入端點常用）
    - 429 / 5xx 與連線錯誤（含 timeout）會指數退避重試；404 與其他 4xx 不重試
    - 回應若是 gzip / deflate 會自動解壓縮
    """
    # path 參數可能含非 ASCII 或特殊字元（例如手誤把創作者名字當 ID 傳進來），
    # 不編碼會在 http.client 層炸出 UnicodeEncodeError，訊息完全看不出原因。
    # safe="/" 保留路徑分隔符，其餘一律百分比編碼。
    url = BASE + urllib.parse.quote(path, safe="/")
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

    headers = {"User-Agent": UA, "Accept": "application/json",
               "Accept-Encoding": "gzip, deflate"}
    if session:
        headers["Cookie"] = f"session={session}"
    data = b"" if method == "POST" else None  # POST 需要 Content-Length: 0；DELETE 不帶 body
    can_retry = retry_on_write or method in _IDEMPOTENT

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with _OPENER.open(req, timeout=30) as r:
                if r.status in (301, 302, 303, 307, 308):
                    raise AuthError(
                        f"HTTP {r.status} for {method} {url} :: 被導向 "
                        f"{r.headers.get('Location')}，通常代表未登入或 session 已失效")
                body = _decompress(r.read(), r.headers.get("Content-Encoding")) \
                    .decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err_body = _decompress(e.read(), e.headers.get("Content-Encoding")) \
                .decode("utf-8", "replace")[:300]
            if e.code in _RETRY_CODES and attempt < retries - 1 and can_retry:
                last = e
                time.sleep(_retry_delay(attempt, e.headers))
                continue
            msg = f"HTTP {e.code} for {method} {url} :: {err_body or '(空 body)'}"
            if e.code == 404:
                raise NotFoundError(msg) from e
            if e.code in (301, 302, 303, 307, 308):
                raise AuthError(msg) from e
            if e.code == 401:
                raise AuthError(msg) from e
            if e.code == 409:
                raise ConflictError(msg) from e
            raise PawchiveError(msg) from e
        except OSError as e:
            # URLError / TimeoutError / ConnectionError 都是 OSError 子類；
            # HTTPError 已在上面處理。涵蓋連線拒絕 / 重置 / 讀取 timeout。
            if attempt < retries - 1 and can_retry:
                last = e
                time.sleep(_retry_delay(attempt))
                continue
            raise PawchiveError(f"連線失敗 {method} {url} :: {e}") from e

        if raw:
            return body
        if not body.strip():
            if empty_ok:
                return None
            raise PawchiveError(f"{method} {url} 回傳空 body，無法解析為 JSON")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise PawchiveError(f"{method} {url} 回傳非 JSON：{body[:200]!r}") from e

    raise PawchiveError(f"重試耗盡 {method} {url} :: {last}")


def get(path, params=None, session=None, raw=False, retries=3):
    """GET 捷徑（保留 v1 的呼叫方式）。"""
    return request("GET", path, params, session, raw=raw, retries=retries)


def _check_offset(offset):
    """後端只接受 50 的倍數；錯誤訊息寫 150 是後端筆誤，別被誤導。"""
    if not isinstance(offset, int) or offset < 0 or offset % PAGE != 0:
        raise ValueError(f"offset 必須是 {PAGE} 的倍數（>= 0），收到 {offset!r}")


# ---------- 公開端點（免登入） ----------

def recent_posts(offset=0, q=None):
    """GET /posts — 全站最新貼文（摘要格式，只有 substring 沒有完整 content）；offset 硬上限 50000"""
    _check_offset(offset)
    if offset > POSTS_OFFSET_CAP:
        raise ValueError(f"/posts 的 offset 硬上限是 {POSTS_OFFSET_CAP}（第 1000 頁）")
    return get("/posts", {"o": offset, "q": q})


def creator_profile(service, creator_id):
    """GET /{service}/user/{creator_id}/profile"""
    return get(f"/{service}/user/{creator_id}/profile")


def creator_posts(service, creator_id, offset=0, q=None):
    """GET /{service}/user/{creator_id} — 回傳完整格式（含 content）；offset 無上限"""
    _check_offset(offset)
    return get(f"/{service}/user/{creator_id}", {"o": offset, "q": q})


def single_post(service, creator_id, post_id):
    """GET /{service}/user/{creator_id}/post/{post_id} — 多了 next / prev 欄位"""
    return get(f"/{service}/user/{creator_id}/post/{post_id}")


def post_comments(service, creator_id, post_id):
    return get(f"/{service}/user/{creator_id}/post/{post_id}/comments")


def post_revisions(service, creator_id, post_id):
    """GET .../revisions — 多數貼文無修訂紀錄，後端回 404 + []，這裡統一回空 list。"""
    try:
        return get(f"/{service}/user/{creator_id}/post/{post_id}/revisions")
    except NotFoundError:
        return []


def is_flagged(service, creator_id, post_id):
    """
    GET .../flag — 回傳 bool。
    後端兩種結果的 body 都是空的（200 = 已標記、404 = 未標記），
    所以必須用 empty_ok 讓 200 空 body 正常通過，靠狀態碼判斷。
    """
    try:
        request("GET", f"/{service}/user/{creator_id}/post/{post_id}/flag", empty_ok=True)
        return True
    except NotFoundError:
        return False


def flag_post(service, creator_id, post_id):
    """
    POST .../flag — 標記貼文要求重新匯入。
    成功回 True；已被標記過（409）回 False，不視為錯誤。
    """
    try:
        request("POST", f"/{service}/user/{creator_id}/post/{post_id}/flag", empty_ok=True)
        return True
    except ConflictError:
        return False


def creator_links(service, creator_id):
    return get(f"/{service}/user/{creator_id}/links")


def announcements(service, creator_id):
    return get(f"/{service}/user/{creator_id}/announcements")


def fancards(service, creator_id):
    """GET .../fancards — 僅 fanbox 有意義，多數情況是空陣列。"""
    return get(f"/{service}/user/{creator_id}/fancards")


def all_creators():
    """GET /creators — 一次吐回全部創作者（~12 MB / 9 萬筆），別在迴圈裡呼叫；indexed/updated 是 Unix timestamp"""
    return get("/creators")


def lookup_hash(sha256):
    """GET /search_hash/{hash} — 命中率不高；查不到回 None（後端是 404 + {}）。"""
    try:
        return get(f"/search_hash/{sha256}")
    except NotFoundError:
        return None


def app_version():
    """GET /app_version — 回傳純文字（目前固定是 "custom"，不是真的 commit hash）。"""
    return get("/app_version", raw=True)


# ---------- 需登入（cookie session） ----------

def favorites_list(session, fav_type="post"):
    """GET /account/favorites — fav_type: "post" 或 "artist"；未帶 session 會 401。"""
    if fav_type not in ("post", "artist"):
        raise ValueError('fav_type 只能是 "post" 或 "artist"')
    return get("/account/favorites", {"type": fav_type}, session=session)


def favorite_post(session, service, creator_id, post_id):
    return request("POST", f"/favorites/post/{service}/{creator_id}/{post_id}",
                   session=session, empty_ok=True)


def unfavorite_post(session, service, creator_id, post_id):
    return request("DELETE", f"/favorites/post/{service}/{creator_id}/{post_id}",
                   session=session, empty_ok=True)


def favorite_creator(session, service, creator_id):
    return request("POST", f"/favorites/creator/{service}/{creator_id}",
                   session=session, empty_ok=True)


def unfavorite_creator(session, service, creator_id):
    return request("DELETE", f"/favorites/creator/{service}/{creator_id}",
                   session=session, empty_ok=True)


# ---------- 分頁 helper ----------

def paginate(fetch, max_items=200, delay=0.5, start=0, offset_cap=None):
    """
    自動翻頁。fetch 是接受 offset 的函式。

    - offset_cap：offset 上限。只有 /posts 要傳 POSTS_OFFSET_CAP；
      創作者貼文列表無上限，保持 None 即可
    - start：起始 offset（必須是 50 的倍數）
    - 後端滿頁才可能有下一頁；回傳空陣列或 < 50 筆即結束
    """
    _check_offset(start)
    out, offset = [], start
    while len(out) < max_items:
        batch = fetch(offset)
        if not batch:
            break
        if not isinstance(batch, list):
            raise PawchiveError(f"分頁端點回傳的不是陣列（{type(batch).__name__}）：{batch!r:.200}")
        out.extend(batch)
        # 已經湊滿或這頁沒滿 = 沒有下一頁；先判斷再 sleep，免得白等最後一次 delay
        if len(batch) < PAGE or len(out) >= max_items:
            break
        offset += PAGE
        if offset_cap is not None and offset > offset_cap:
            break
        time.sleep(delay)
    return out[:max_items]


# ---------- 檔案網址 ----------

def file_url(path, name=None):
    """把 API 回傳的 file/attachment path 轉成可下載的網址（支援 Range / 斷點續傳）。"""
    url = f"{FILE_CDN}/data{path}"
    if name:
        url += "?f=" + urllib.parse.quote(name)
    return url


def thumb_url(path):
    return f"{THUMB_CDN}/thumbnail/data{path}"


def _ext_of(path):
    """取副檔名當預設檔名的尾巴；path 是 sha256 命名，副檔名不會太長。"""
    ext = os.path.splitext(path)[1]
    return ext if 0 < len(ext) <= 10 else ""


def post_files(post, include_cover=True, dedupe=True):
    """
    收集一篇貼文裡所有檔案（封面 + 附件），回傳
    [{"name": 檔名, "path": API 路徑, "url": 原始檔網址, "thumb": 縮圖網址}, ...]。

    - include_cover=False：跳過 file（封面）欄位，只取 attachments
    - dedupe=True：同一個 path 只回傳一次。實測 /posts 首頁 50 篇裡有 29 篇的
      封面與某個 attachment 指向同一個檔案，不去重等於每篇多抓一份一模一樣的圖。
    - 無檔名時用 {post_id}_{n}{副檔名} 當預設檔名，批次下載存檔不會互相覆蓋。
    """
    out, seen = [], set()
    pid = post.get("id", "post")

    def add(entry, fallback):
        path = (entry or {}).get("path")
        if not path or (dedupe and path in seen):
            return
        seen.add(path)
        name = (entry.get("name") or "").strip() or (fallback + _ext_of(path))
        out.append({"name": name, "path": path,
                    "url": file_url(path, name), "thumb": thumb_url(path)})

    if include_cover:
        add(post.get("file"), f"{pid}_cover")
    for i, a in enumerate(post.get("attachments") or []):
        add(a, f"{pid}_{i}")
    return out


def post_file_urls(post, include_cover=True, dedupe=True):
    """收集一篇貼文裡所有檔案的 (檔名, 下載網址)；細節與參數見 post_files()。"""
    return [(f["name"], f["url"]) for f in post_files(post, include_cover, dedupe)]


# ---------- CLI ----------

def _need_session(a):
    if not a.session:
        raise SystemExit("錯誤: 此操作需要 session，請用 --session 參數或設定環境變數 PAWCHIVE_SESSION")
    return a.session


def main():
    ap = argparse.ArgumentParser(description="Pawchive API v1 client v3")
    ap.add_argument("--session", default=os.environ.get("PAWCHIVE_SESSION"),
                    help="登入後 cookie 裡的 session 值（或用環境變數 PAWCHIVE_SESSION）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("recent"); p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("search"); p.add_argument("query")
    p.add_argument("--max", type=int, default=50); p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("profile"); p.add_argument("service"); p.add_argument("creator_id")
    p = sub.add_parser("posts"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("--max", type=int, default=50)
    p = sub.add_parser("post"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("comments"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("revisions"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("links"); p.add_argument("service"); p.add_argument("creator_id")
    p = sub.add_parser("announcements"); p.add_argument("service"); p.add_argument("creator_id")
    p = sub.add_parser("fancards"); p.add_argument("service"); p.add_argument("creator_id")
    p = sub.add_parser("urls"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("hash"); p.add_argument("sha256")
    p = sub.add_parser("isflagged"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("flag"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    sub.add_parser("version")

    p = sub.add_parser("favorites"); p.add_argument("--type", choices=["post", "artist"], default="post")
    p = sub.add_parser("favpost"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("unfavpost"); p.add_argument("service"); p.add_argument("creator_id"); p.add_argument("post_id")
    p = sub.add_parser("favcreator"); p.add_argument("service"); p.add_argument("creator_id")
    p = sub.add_parser("unfavcreator"); p.add_argument("service"); p.add_argument("creator_id")

    a = ap.parse_args()
    dump = lambda x: print(json.dumps(x, ensure_ascii=False, indent=2))
    try:
        if a.cmd == "recent":
            for x in recent_posts(a.offset):
                print(f"[{x['service']}/{x['user']}] {x['id']}  {x['published']}  {x['title'][:60]}")
        elif a.cmd == "search":
            rows = paginate(lambda o: recent_posts(o, q=a.query), max_items=a.max,
                            start=a.offset, offset_cap=POSTS_OFFSET_CAP)
            for x in rows:
                print(f"[{x['service']}/{x['user']}] {x['id']}  {x['title'][:60]}")
            print(f"\n共 {len(rows)} 筆")
        elif a.cmd == "profile":
            dump(creator_profile(a.service, a.creator_id))
        elif a.cmd == "posts":
            rows = paginate(lambda o: creator_posts(a.service, a.creator_id, o), max_items=a.max)
            for x in rows:
                n = len(x.get("attachments") or [])
                print(f"{x['id']}  {x['published']}  att={n:<3} {x['title'][:55]}")
            print(f"\n共 {len(rows)} 筆")
        elif a.cmd == "post":
            dump(single_post(a.service, a.creator_id, a.post_id))
        elif a.cmd == "comments":
            for c in post_comments(a.service, a.creator_id, a.post_id):
                print(f"- {c.get('commenter_name')} ({c.get('published')}): {c.get('content', '')[:100]}")
        elif a.cmd == "revisions":
            dump(post_revisions(a.service, a.creator_id, a.post_id))
        elif a.cmd == "links":
            dump(creator_links(a.service, a.creator_id))
        elif a.cmd == "announcements":
            dump(announcements(a.service, a.creator_id))
        elif a.cmd == "fancards":
            dump(fancards(a.service, a.creator_id))
        elif a.cmd == "urls":
            for name, url in post_file_urls(single_post(a.service, a.creator_id, a.post_id)):
                print(f"{name}\t{url}")
        elif a.cmd == "hash":
            r = lookup_hash(a.sha256)
            dump(r) if r is not None else print("查無此檔案（此索引涵蓋範圍不完整，404 屬常態）")
        elif a.cmd == "isflagged":
            print("已標記" if is_flagged(a.service, a.creator_id, a.post_id) else "未標記")
        elif a.cmd == "flag":
            print("已送出標記" if flag_post(a.service, a.creator_id, a.post_id) else "此貼文先前已被標記過（409）")
        elif a.cmd == "version":
            print(app_version())
        elif a.cmd == "favorites":
            dump(favorites_list(_need_session(a), a.type))
        elif a.cmd == "favpost":
            favorite_post(_need_session(a), a.service, a.creator_id, a.post_id); print("已收藏貼文")
        elif a.cmd == "unfavpost":
            unfavorite_post(_need_session(a), a.service, a.creator_id, a.post_id); print("已取消收藏貼文")
        elif a.cmd == "favcreator":
            favorite_creator(_need_session(a), a.service, a.creator_id); print("已收藏創作者")
        elif a.cmd == "unfavcreator":
            unfavorite_creator(_need_session(a), a.service, a.creator_id); print("已取消收藏創作者")
    except (PawchiveError, ValueError) as e:
        raise SystemExit(f"錯誤: {e}")


if __name__ == "__main__":
    main()
