# Pawchive API v1 使用指南

> 研究日期：2026-07-28 · 所有端點皆已實測驗證
> 隨附檔案：
> - `pawchive_教學.md` — **新手從這裡開始**，手把手操作教學
> - `pawchive_gui.py` — 圖形化介面（本機 Web GUI）
> - `pawchive_client_v3.py` — API client（可當指令用也可 `import`）
> - `pawchive_download.py` — 批次下載器（支援斷點續傳）
> - `pawchive_openapi.json` — 完整 OpenAPI 3.0.1 規格
> - `test_v3_mock.py` — 邊界情境測試
>
> **本文件是參考手冊**（查端點、欄位、陷阱）。要照著做的操作步驟請看 `pawchive_教學.md`。

---

## 0. 先講重點

`https://pawchive.pw/api/schema` 本身**不是** JSON 規格檔，它是一個 Swagger UI 的 HTML 外殼。真正的 OpenAPI JSON 內嵌在 iframe 頁面 `https://pawchive.pw/api/swagger_schema` 的 `<script>` 裡（變數名 `swagger_spec`）。我已經幫你抽出來存成 `pawchive_openapi.json`。

| 項目 | 內容 |
|---|---|
| Base URL | `https://pawchive.pw/api/v1` |
| 規格版本 | OpenAPI 3.0.1 / API 1.0.0 |
| 端點數 | 19 個操作（16 個路徑） |
| 認證 | 只有 Favorites 需要，用 cookie `session` |
| 回應格式 | `application/json`，**沒有** envelope，直接就是陣列或物件 |
| 收錄服務 | 僅 `patreon`（66,306 位）與 `fanbox`（24,126 位），共 90,432 位創作者 |

這是 Kemono 系列的分支站，資料結構跟 Kemono API 幾乎一模一樣，若你寫過 Kemono 的爬蟲可以直接沿用。

---

## 1. 端點總覽

### 免登入（15 個）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/creators` | 全部創作者清單（一次性 12 MB） |
| GET | `/posts` | 全站最新貼文 / 搜尋 |
| GET | `/{service}/user/{creator_id}` | 某創作者的貼文列表 |
| GET | `/{service}/user/{creator_id}/profile` | 創作者資料 |
| GET | `/{service}/user/{creator_id}/links` | 創作者的關聯帳號 |
| GET | `/{service}/user/{creator_id}/announcements` | 創作者公告 |
| GET | `/{service}/user/{creator_id}/fancards` | 粉絲卡（**僅 fanbox**） |
| GET | `/{service}/user/{creator_id}/post/{post_id}` | 單篇貼文 |
| GET | `/{service}/user/{creator_id}/post/{post_id}/comments` | 貼文留言 |
| GET | `/{service}/user/{creator_id}/post/{post_id}/revisions` | 貼文修訂歷史 |
| GET | `/{service}/user/{creator_id}/post/{post}/flag` | 查詢貼文是否被標記 |
| POST | `/{service}/user/{creator_id}/post/{post}/flag` | 標記貼文要求重新匯入 |
| GET | `/search_hash/{file_hash}` | 用 SHA-256 反查檔案 |
| GET | `/app_version` | 目前部署的 commit hash |

### 需登入（4 個，`cookieAuth`）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/account/favorites?type=post\|artist` | 列出收藏 |
| POST / DELETE | `/favorites/post/{service}/{creator_id}/{post_id}` | 收藏／取消收藏貼文 |
| POST / DELETE | `/favorites/creator/{service}/{creator_id}` | 收藏／取消收藏創作者 |

---

## 2. 分頁規則（這裡有坑）

分頁只有一個參數 `o`（offset），**沒有 limit**，每頁固定 50 筆。

實測結果與官方文件有出入，請以實測為準：

```
GET /posts?o=17        → 400  {"error":"offset not multiple of 150 or too large"}
GET /posts?o=50        → 200  50 筆   ← 錯誤訊息寫 150，實際 50 就過
GET /posts?o=50000     → 200  50 筆
GET /posts?o=50050     → 400  已超上限
```

**要點：**
- `o` 必須是 **50 的倍數**（錯誤訊息裡的「150」是後端寫錯，別被誤導）
- `/posts` 的 offset 硬上限是 **50,000**，也就是最多只能翻到第 1000 頁
- `/{service}/user/{id}` 的 offset 沒有上限，但超出該創作者貼文總數後回傳空陣列 `[]`
- 判斷結束的方式：回傳筆數 < 50，或收到空陣列

搜尋參數 `q` 可與 `o` 併用：`/posts?q=genshin&o=50`。

---

## 3. 兩種貼文格式（很容易踩雷）

同樣是「貼文」，不同端點回的欄位不一樣：

**摘要格式**（只有 `/posts` 用）
```json
{
  "id": "12323115", "user": "21971914", "service": "fanbox",
  "title": "...",
  "substring": "<p>前 50 字左右的預覽</p>",
  "published": "2026-07-28T15:39:07",
  "file": {"name": "cover.jpeg", "path": "/b5/12/b512af55....jpeg"},
  "attachments": [],
  "preview_state": "scraped", "has_full": true, "origin": "import"
}
```

**完整格式**（`/{service}/user/{id}` 和單篇貼文都用這個）
```json
{
  "id": "...", "user": "...", "service": "...", "title": "...",
  "content": "<p>完整 HTML 內文</p>",
  "embed": {}, "shared_file": false,
  "added": "2026-07-28T06:39:07",       // 入庫時間（UTC）
  "published": "2026-07-28T15:39:07",   // 原站發布時間（原站時區）
  "edited": "2026-07-28T15:39:07",
  "file": {...}, "attachments": [...],
  "poll": null, "captions": null, "tags": null,
  "origin": "import", "preview_state": "scraped", "has_full": true,
  "detail_fetched": true,
  "next": "12317205", "prev": null      // ← 只有單篇貼文端點才有
}
```

所以：想拿完整內文，**別用 `/posts`**，改用創作者貼文列表或單篇端點。`next`/`prev` 讓你可以不靠列表就順著時間軸爬完一位創作者。

`content` 是原始 HTML，含 `<p>`、`<a>` 等標籤，要自己清理或渲染。

---

## 4. 檔案下載（規格書完全沒寫）

API 回傳的 `file.path` / `attachments[].path` 只是相對路徑，**必須自己組 CDN 網址**，而且不是主站網域：

```
原始檔： https://file.pawchive.pw/data{path}?f={原始檔名}
縮圖：   https://img.pawchive.pw/thumbnail/data{path}
```

範例：
```
path = /b5/12/b512af5580645995c8e72fb00278bde7c7624ed71e72e860ee271aa8ea17a7ae.jpeg
→ https://file.pawchive.pw/data/b5/12/b512af55...jpeg?f=cover.jpeg   ✅ 200, image/jpeg
```

**實測補充：**
- 主站 `https://pawchive.pw/data{path}` 會回 **404**，一定要用 `file.` 子網域
- `?f=` 只是給瀏覽器決定下載檔名用的，省略也能拿到檔案
- `file.pawchive.pw` **支援 Range 請求**（回 `206` + `Content-Range`），可以斷點續傳或分段下載
- 縮圖 CDN 偶爾回 `502`，做好 fallback 到原始檔
- 路徑的前兩層目錄就是 SHA-256 的前 4 個字元，檔名本身 = 該檔案的 SHA-256

---

## 5. 認證方式

`securitySchemes` 只定義了一種：

```json
{"cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session",
  "description": "Session key that can be found in cookies after a successful login"}}
```

**沒有 API key、沒有 Bearer token、沒有 OAuth。** 唯一取得方式是用瀏覽器登入 `https://pawchive.pw/account/login`（表單欄位 `username` / `password`），從 cookie 抓出 `session` 的值，之後手動帶：

```
Cookie: session=<你的 session 值>
```

未帶 cookie 呼叫 `/account/favorites` 會回 `401` 加一個空物件 `{}`。

**規格書寫 302，實際回 401。** 規格宣稱 favorites 的寫入端點未認證時會「Redirect to login」，但實測（無 cookie 與帶無效 cookie 兩種情況）**一律回 401**，沒有 `Location` header。不過 client 仍應同時處理 302，因為 session 過期的行為可能不同於完全未帶 cookie——尤其在 Python `urllib` 下，**302 對 GET/POST 會被自動跟隨**（`HTTPRedirectHandler.redirect_request` 允許 301/302/303 + POST，並把方法改寫成 GET），結果就是拿到登入頁的 HTML 而不是錯誤，DELETE 則因不在允許清單而正常拋出 `HTTPError`。同一種失敗在不同 HTTP 方法下表現不一致，很難除錯。解法是掛一個不跟隨轉址的 opener。

注意：`/api/v1/account/login` 這種端點**不存在**（404），登入不走 API，只能走網頁表單。

---

## 6. 錯誤處理

| 狀態碼 | 回應內容 | 情境 |
|---|---|---|
| 400 | `{"error":"offset not multiple of 50"}` | 創作者貼文 offset 錯誤 |
| 400 | `{"error":"offset not multiple of 150 or too large"}` | `/posts` offset 錯誤或超過 50000 |
| 401 | `{}` | 未登入存取 favorites |
| 404 | `{"error":"Creator not found."}` | 創作者不存在 |
| 404 | `[]` | 貼文無修訂紀錄（**回空陣列不是物件**） |
| 404 | 空字串 | flag 端點查無標記（**200 也是空 body**，見下） |
| 404 | `{}` | search_hash 查無檔案 |
| 409 | — | 重複 flag 同一篇貼文 |

錯誤格式不統一（有時 `{"error":...}`、有時 `{}`、有時 `[]`、有時空字串），解析時務必包 try/except，別假設 body 一定是合法 JSON 物件。

**`/flag` 的 GET 兩種結果 body 都是空的**：規格裡 `200`（已標記）與 `404`（未標記）的 `content` 都是 `{}`。也就是說判斷「這篇有沒有被標記」**只能看狀態碼，不能解析 body**——如果你的 client 遇到 2xx 就無條件 `json.loads()`，「已標記」這個成功結果反而會炸成解析錯誤。同理 `POST /flag` 成功是 `201` 空 body。

**409 不該當成失敗**：重複標記同一篇貼文會回 409，語意是「先前已標記過」，對呼叫端來說通常等同成功，建議單獨分類而不是丟通用錯誤。

---

## 7. 其他實測發現

- **無速率限制跡象**：連續 10 次快速請求全部 200，沒有 `X-RateLimit-*` header。但仍建議自律加 0.5 秒延遲，這是無商業支援的鏡像站。
- **有 CDN 快取**：`/posts` 回應帶 `cache-control: max-age=60`，`vary: Cookie`。重複打同一 URL 60 秒內拿到的是快取。
- **`/creators` 很重**：12.4 MB、90,432 筆，一次全吐。規格書自己都警告「don't use the try it out button or your page will crash」。抓一次存本地就好。注意它的 `indexed`/`updated` 是 **Unix timestamp 整數**，跟 `/profile` 端點回的 **ISO 8601 字串**格式不同 —— 同一概念兩種型別，很容易踩雷。
- **`/app_version` 回傳純文字** `custom`，不是 JSON、也不是真的 commit hash。
- **`search_hash` 命中率低**：我拿站上實際貼文的檔案 hash 去查，兩次都是 404。這個索引似乎不完整或只涵蓋部分檔案。
- **`revisions` / `fancards` / `links` 常常是空的**：多數貼文沒有修訂紀錄，fancards 只有 fanbox 且大多為 `[]`。
- **網頁上的功能不等於有 API**：`/posts/popular`、`/posts/tags`、`/posts/random`、`/dms`、`/account/keys` 這些網頁選單上的功能**都沒有對應的 v1 API 端點**（全 404）。要用只能爬 HTML。
- **API 沒有 CORS 標頭**：實測帶 `Origin` 呼叫 `/api/v1/posts`，回應中沒有任何 `access-control-*`。這代表**瀏覽器端的 JS 無法直接呼叫此 API**，網頁應用必須自建後端代理。但 `file.pawchive.pw` 有回 `access-control-allow-origin: *`，所以圖片可以讓前端直連。
- **鏡像站**：頁面 canonical 指向 `pawchive.st`，但該網域對 API 請求回 301。建議直接用 `pawchive.pw`。

---

## 8. 寫 client 時的三個陷阱

這三點是實際踩過才會發現的，規格書都沒寫：

**1. 不要對 POST 做自動重試。** 遇到 5xx 就重試是常見寫法，但 `POST /flag` 非冪等——重送會得到 409。更危險的是 5xx 有可能發生在伺服器已處理完、只是回應失敗的當下，這時重試等於送出第二次寫入。建議只對 `GET`/`HEAD`/`DELETE`（冪等）自動重試，POST 留給呼叫端決定。

**2. 關掉 urllib 的自動轉址。** 如上一節所述，`urllib` 會讓 POST 跟隨 302 並改寫成 GET，把「認證失敗」偽裝成一個 200 的 HTML 回應。掛 `HTTPRedirectHandler` 子類讓 `redirect_request` 回 `None` 即可。

**3. 空 body 不等於錯誤。** 本 API 有多個端點成功時就是回空 body（`200 /flag`、`201 POST /flag`、favorites 寫入），要區分「成功但無內容」與「回應格式異常」。

**4. path 參數要先做 URL 編碼。** `creator_id` / `post_id` 雖然實務上都是數字，但只要呼叫端手誤傳進非 ASCII 字串（例如把創作者名字當成 ID），未編碼的 URL 會在 `http.client` 底層炸出 `UnicodeEncodeError: 'ascii' codec can't encode characters`——堆疊十幾層深、完全看不出是參數問題。用 `urllib.parse.quote(path, safe="/")` 包一下，就會正常變成 404。

```python
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # 302 在本 API 代表 session 失效，別跟過去

_OPENER = urllib.request.build_opener(_NoRedirect)
_IDEMPOTENT = {"GET", "HEAD", "DELETE"}   # POST 不自動重試

url = BASE + urllib.parse.quote(path, safe="/")   # 避免非 ASCII 參數炸在底層
```

---

## 9. 快速上手

```bash
# 最新貼文
curl -s "https://pawchive.pw/api/v1/posts" | jq '.[0]'

# 搜尋
curl -s "https://pawchive.pw/api/v1/posts?q=genshin&o=0" | jq 'length'

# 創作者資料
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914/profile" | jq

# 單篇貼文
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914/post/12323115" | jq '.title, .next'

# 收藏（需 session）
curl -s -H "Cookie: session=XXX" "https://pawchive.pw/api/v1/account/favorites?type=post"
```

隨附的 Python client（`pawchive_client_v3.py`，涵蓋規格全部 19 個操作）：

```bash
python3 pawchive_client_v3.py recent
python3 pawchive_client_v3.py search "genshin" --max 100
python3 pawchive_client_v3.py profile fanbox 21971914
python3 pawchive_client_v3.py posts fanbox 21971914 --max 120   # 自動翻頁
python3 pawchive_client_v3.py urls fanbox 120884008 12322732    # 印出下載網址
python3 pawchive_client_v3.py isflagged fanbox 21971914 12323115
python3 pawchive_client_v3.py --session XXX favorites --type post
```

內建自動翻頁、offset 事前驗證、只對冪等方法重試、不跟隨轉址、CDN 網址組裝，以及分類過的例外
（`NotFoundError` / `AuthError` / `ConflictError`，皆繼承 `PawchiveError`）。可直接 `import` 當函式庫：

```python
from pawchive_client_v3 import creator_posts, paginate, post_file_urls

rows = paginate(lambda o: creator_posts("fanbox", "21971914", o), max_items=200)
for post in rows:
    for name, url in post_file_urls(post):
        print(name, url)
```

---

## 10. 內容性質提醒

這是一個聚合 Patreon / Fanbox 付費內容的鏡像站，站上以成人向內容為主，且內容多為未經創作者授權的重製品。使用前請自行評估法律與合規風險，尤其是若打算把抓下來的資料用於再散布或商業用途。技術上 API 沒有任何存取限制，但「能抓」不等於「能用」。
