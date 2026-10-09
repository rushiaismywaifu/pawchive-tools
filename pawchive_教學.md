# Pawchive API 使用教學

> 從零開始，照著做就會動。所有指令都已實測。
>
> 想查端點細節、欄位定義、踩坑紀錄 → 看 `pawchive_api_guide.md`（參考手冊）
> 這份是**操作教學**，帶你把常見任務跑一遍。

---

## 目錄

0. [圖形化介面（最簡單）](#0-圖形化介面最簡單)
1. [5 分鐘上手](#1-5-分鐘上手)
2. [不寫程式：直接用 curl](#2-不寫程式直接用-curl)
3. [用命令列工具](#3-用命令列工具)
4. [批次下載創作者作品](#4-批次下載創作者作品)
5. [寫自己的 Python 程式](#5-寫自己的-python-程式)
6. [登入後才能用的功能](#6-登入後才能用的功能)
7. [常見情境速查](#7-常見情境速查)
8. [疑難排解](#8-疑難排解)

---

## 0. 圖形化介面（最簡單）

不想碰指令列的話，直接開圖形介面：

```bash
python3 pawchive_gui.py
```

瀏覽器會自動打開 `http://127.0.0.1:8765`，接下來全部用滑鼠操作：

- **最新貼文** — 縮圖牆，捲到底按「載入更多」翻頁
- **找創作者** — 打名字搜尋 9 萬位創作者，點一下看他的全部作品
- **點縮圖** — 開啟詳情：完整內文、所有圖片、留言、上一篇／下一篇
- **下載** — 在創作者頁面按「下載這位創作者…」，右下角會顯示即時進度，可隨時取消
- **模糊預覽** — 右上角的勾選框，**預設開啟**，所有圖片會打上馬賽克，方便在公共場合瀏覽。
  覺得圖片糊掉不是你要的，把勾取消即可，設定會被瀏覽器記住

其他選項：

```bash
python3 pawchive_gui.py --port 9000     # 換連接埠
python3 pawchive_gui.py --no-browser    # 不自動開瀏覽器
```

按 `Ctrl+C` 結束。**只在本機執行，不會對外開放。**

> **為什麼需要跑一個本機伺服器？**
> Pawchive 的 API 沒有回傳 CORS 標頭，瀏覽器裡的 JS 無法直接呼叫它。
> 所以 `pawchive_gui.py` 在本機當代理。圖片 CDN 則有 `access-control-allow-origin: *`，
> 前端直連、不經過代理，縮圖載入才會快。

---

## 1. 5 分鐘上手

### 環境需求

只需要 **Python 3.8 以上**，不用裝任何套件（全部使用標準函式庫）。

```bash
python3 --version    # 確認有 Python 3
```

### 檔案說明

| 檔案 | 用途 |
|---|---|
| `pawchive_gui.py` | **圖形化介面**，用瀏覽器點選操作 |
| `pawchive_client_v3.py` | API client，可當指令用也可當函式庫 `import` |
| `pawchive_download.py` | 批次下載器，抓創作者的貼文與附件 |
| `pawchive_api_guide.md` | API 參考手冊（端點、欄位、陷阱） |
| `pawchive_openapi.json` | 原始 OpenAPI 規格 |
| `test_v3_mock.py` | 邊界情境測試（改動 client 後可重跑） |

### 第一個指令

```bash
python3 pawchive_client_v3.py recent
```

會列出全站最新 50 篇貼文：

```
[fanbox/21971914] 12323115  2026-07-28T15:39:07  しずかちゃんの保健体育♡
[fanbox/120884008] 12322732  2026-07-28T14:00:34  Himeko • Nova 姬子•启行 ...
```

方括號裡是 `服務/創作者ID`，後面是 `貼文ID`、發布時間、標題。**這三個 ID 是後面所有操作的鑰匙。**

### 三個核心概念

任何一筆資料都由三個值定位：

```
service      = patreon 或 fanbox        （只有這兩種）
creator_id   = 創作者在原站的數字 ID
post_id      = 貼文在原站的數字 ID
```

比方說網址 `https://pawchive.pw/fanbox/user/21971914/post/12323115` 就對應：

```
service=fanbox   creator_id=21971914   post_id=12323115
```

**從網頁找 ID 最快**：在 pawchive 網站上打開任何創作者或貼文，網址列直接就有。

---

## 2. 不寫程式：直接用 curl

想快速試一下、或用別的語言接，直接打 HTTP 就好。Base URL 是 `https://pawchive.pw/api/v1`。

```bash
# 最新貼文
curl -s "https://pawchive.pw/api/v1/posts"

# 搭配 jq 好讀（沒裝 jq 可省略）
curl -s "https://pawchive.pw/api/v1/posts" | jq '.[0]'

# 搜尋
curl -s "https://pawchive.pw/api/v1/posts?q=genshin"

# 創作者資料
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914/profile"

# 某創作者的貼文（這個才有完整內文）
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914"

# 單篇貼文
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914/post/12323115"

# 留言
curl -s "https://pawchive.pw/api/v1/fanbox/user/21971914/post/12323115/comments"
```

**翻頁用 `o` 參數，一頁固定 50 筆，數值必須是 50 的倍數**：

```bash
curl -s "https://pawchive.pw/api/v1/posts?o=0"     # 第 1 頁
curl -s "https://pawchive.pw/api/v1/posts?o=50"    # 第 2 頁
curl -s "https://pawchive.pw/api/v1/posts?o=100"   # 第 3 頁
```

傳非 50 倍數會被打回 `400`。（錯誤訊息會說「not multiple of 150」，那是後端寫錯，實際是 50。）

---

## 3. 用命令列工具

`pawchive_client_v3.py` 把上面那些包成好記的指令。

### 瀏覽與搜尋

```bash
# 最新貼文
python3 pawchive_client_v3.py recent

# 翻到第 2 頁
python3 pawchive_client_v3.py recent --offset 50

# 搜尋，自動翻頁抓 100 筆
python3 pawchive_client_v3.py search "genshin" --max 100
```

### 看創作者

```bash
# 基本資料
python3 pawchive_client_v3.py profile fanbox 21971914
```
```json
{
  "id": "21971914",
  "name": "samunekochan",
  "service": "fanbox",
  "indexed": "2026-06-10T20:38:52.719107",
  "updated": "2026-07-28T10:06:41.204239",
  "kemono_favorited": 8663
}
```

```bash
# 貼文列表（自動翻頁）
python3 pawchive_client_v3.py posts fanbox 21971914 --max 120

# 公告、關聯帳號、粉絲卡
python3 pawchive_client_v3.py announcements fanbox 21971914
python3 pawchive_client_v3.py links fanbox 21971914
python3 pawchive_client_v3.py fancards fanbox 21971914
```

### 看單篇貼文

```bash
# 完整內容（含 HTML 內文）
python3 pawchive_client_v3.py post fanbox 21971914 12323115

# 留言
python3 pawchive_client_v3.py comments fanbox 21971914 12323115

# 取得檔案下載網址 ← 常用
python3 pawchive_client_v3.py urls fanbox 120884008 12322732
```

最後一個會輸出可直接下載的網址：

```
cover.jpeg    https://file.pawchive.pw/data/af/6e/af6e72...jpeg?f=cover.jpeg
Xk1VfX...png  https://file.pawchive.pw/data/e1/39/e13958...png?f=Xk1VfX...png
```

搭配 `wget` 就能下載：

```bash
python3 pawchive_client_v3.py urls fanbox 120884008 12322732 | cut -f2 | wget -i -
```

### 其他

```bash
python3 pawchive_client_v3.py version                              # 部署版本
python3 pawchive_client_v3.py isflagged fanbox 21971914 12323115   # 查是否被標記
python3 pawchive_client_v3.py hash <sha256>                        # 用檔案雜湊反查
```

---

## 4. 批次下載創作者作品

這是最常見的需求，`pawchive_download.py` 專門做這件事。

### 務必先用 --dry-run 試跑

```bash
python3 pawchive_download.py fanbox 120884008 --max 10 --dry-run
```

`--dry-run` **不寫任何檔案**，只告訴你會下載什麼、各多大：

```
創作者：tomoki hoyoo (fanbox/120884008)
輸出至：downloads/fanbox_tomoki hoyoo_120884008   [DRY RUN 不寫檔]

取得 2 篇貼文

[1/2] 2026-07-28  Himeko • Nova 姬子•启行 姫子ひめこ・旅立たびだち
      cover.jpeg  (116.6KB)
      Xk1VfXhRvhM44mdtWUQPJ6PY.png  (1.1MB)
      RheQaJ06zJXMY7XXWf5IpjzJ.png  (1.1MB)
```

確認沒問題後拿掉 `--dry-run` 就會實際下載。

### 實際下載

```bash
python3 pawchive_download.py fanbox 120884008 --max 10
```

```
[1/1] 2026-07-28  Himeko • Nova 姬子•启行 姫子ひめこ・旅立たびだち
      ✓ cover.jpeg  (116.6KB)
      ✓ Xk1VfXhRvhM44mdtWUQPJ6PY.png  (1.1MB)
      ✓ RheQaJ06zJXMY7XXWf5IpjzJ.png  (1.1MB)

──────────────────────────────────────────────────
完成：新增 3、續傳 0、跳過 0、超過上限 0、失敗 0
本次下載量：2.3MB
```

符號意義：`✓` 新下載、`↻` 續傳、`·` 已存在跳過、`✗` 失敗或超過大小上限。

### 目錄結構

```
downloads/
└── fanbox_tomoki hoyoo_120884008/
    └── 2026-07-28_12322732_Himeko • Nova 姬子•启行/
        ├── post.json          ← 完整貼文資料（標題、內文 HTML、時間…）
        ├── cover.jpeg
        ├── Xk1VfXhRvhM44mdtWUQPJ6PY.png
        └── RheQaJ06zJXMY7XXWf5IpjzJ.png
```

### 中斷了可以直接重跑

**斷點續傳是內建的。** 網路斷掉、按了 Ctrl+C，或只是想抓新的貼文，重跑同一條指令即可：

- 已下載完整的檔案 → 跳過（`·`）
- 下載到一半的檔案 → 從中斷處續傳（`↻`）
- 新的貼文 → 正常下載（`✓`）

實測：把一個 1.1MB 的檔案截斷成 400KB 再重跑，會續傳補完，SHA-256 與原檔完全相符。

### 常用選項

```bash
# 指定輸出目錄
python3 pawchive_download.py fanbox 120884008 --out ~/我的下載

# 跳過封面圖，只抓附件
python3 pawchive_download.py fanbox 120884008 --no-cover

# 只存貼文文字（post.json），完全不下載圖檔
python3 pawchive_download.py fanbox 120884008 --max 100 --metadata-only

# 單檔超過 50MB 就跳過（避免抓到巨大影片）
python3 pawchive_download.py fanbox 120884008 --max-mb 50

# 放慢速度，對站方友善一點
python3 pawchive_download.py fanbox 120884008 --delay 2

# 一篇貼文內同時抓 6 個檔案（圖多的創作者會快很多）
python3 pawchive_download.py fanbox 120884008 --workers 6
```

| 選項 | 說明 | 預設 |
|---|---|---|
| `--max N` | 最多處理幾篇貼文 | 20 |
| `--out DIR` | 輸出目錄 | `downloads` |
| `--dry-run` | 只列出不寫檔 | 關 |
| `--no-cover` | 跳過封面（`file` 欄位），只抓 attachments | 關 |
| `--metadata-only` | 只存 post.json | 關 |
| `--max-mb N` | 單檔大小上限（MB），0 = 不限 | 0 |
| `--delay N` | 每篇之間間隔秒數 | 0.5 |
| `--workers N` | 同一篇內同時下載幾個檔案 | 3 |

`--workers` 只影響檔案下載（走 `file.pawchive.pw`），API 查詢一律照 `--delay` 循序進行，
所以調高它不會加重 API 的負擔。設成 1 就是舊版的完全循序行為。

### 重複的封面只會抓一次

這個 API 有個容易踩到的地方：**多數貼文的 `file`（封面）跟 `attachments` 裡的某一項
其實是同一個檔案**（實測全站最新 50 篇裡有 29 篇如此）。
下載器會依 `path` 去重，同一個檔案只抓一次、只存一份。

---

## 5. 寫自己的 Python 程式

把 client 當函式庫用。**注意程式要放在跟 `pawchive_client_v3.py` 同一個目錄。**

### 最小範例

```python
import pawchive_client_v3 as pc

# 最新貼文
for p in pc.recent_posts():
    print(p["title"])

# 創作者資料
prof = pc.creator_profile("fanbox", "21971914")
print(prof["name"])

# 單篇貼文
post = pc.single_post("fanbox", "21971914", "12323115")
print(post["content"])     # HTML 內文
```

### 自動翻頁

`paginate()` 幫你處理 offset 遞增與結束判斷：

```python
from pawchive_client_v3 import creator_posts, paginate

# 抓某創作者最新 200 篇
rows = paginate(
    lambda o: creator_posts("fanbox", "21971914", o),
    max_items=200,
    delay=0.5,          # 每頁之間停 0.5 秒
)
print(f"共 {len(rows)} 篇")
```

搜尋全站要多帶 `offset_cap`（`/posts` 有 50000 的硬上限）：

```python
from pawchive_client_v3 import recent_posts, paginate, POSTS_OFFSET_CAP

rows = paginate(
    lambda o: recent_posts(o, q="genshin"),
    max_items=500,
    offset_cap=POSTS_OFFSET_CAP,
)
```

### 取得檔案網址

```python
from pawchive_client_v3 import single_post, post_file_urls

post = single_post("fanbox", "120884008", "12322732")
for name, url in post_file_urls(post):
    print(name, "→", url)
```

### 錯誤處理

例外都繼承 `PawchiveError`，可以分開接也可以一次接：

```python
import pawchive_client_v3 as pc

try:
    prof = pc.creator_profile("fanbox", "不存在的ID")
except pc.NotFoundError:
    print("找不到這個創作者")
except pc.AuthError:
    print("需要登入或 session 過期")
except pc.ConflictError:
    print("狀態衝突（例如重複標記）")
except pc.PawchiveError as e:
    print("其他 API 錯誤:", e)
```

| 例外 | 觸發時機 |
|---|---|
| `NotFoundError` | 404 — 創作者、貼文、雜湊找不到 |
| `AuthError` | 401 / 302 — 未登入或 session 失效 |
| `ConflictError` | 409 — 重複標記等狀態衝突 |
| `PawchiveError` | 其他（連線失敗、非預期格式）。**以上三者的父類** |

### 完整範例：找出附件最多的貼文

```python
import pawchive_client_v3 as pc

rows = pc.paginate(
    lambda o: pc.creator_posts("fanbox", "120884008", o),
    max_items=100,
)

ranked = sorted(rows, key=lambda p: len(p.get("attachments") or []), reverse=True)
for p in ranked[:5]:
    n = len(p.get("attachments") or [])
    print(f"{n:3d} 個附件  {p['published'][:10]}  {p['title'][:40]}")
```

---

## 6. 登入後才能用的功能

「收藏」相關的 4 個端點需要登入。**這個 API 沒有 API key**，只能用瀏覽器登入後的 session cookie。

### 取得 session

1. 瀏覽器開 `https://pawchive.pw/account/login` 登入
2. 按 **F12** 開開發者工具
3. 到 **Application**（Chrome）或 **Storage**（Firefox）分頁
4. 左側找 **Cookies** → `https://pawchive.pw`
5. 複製名為 `session` 的那一列的值

### 使用

建議放環境變數，避免出現在指令歷史裡：

```bash
export PAWCHIVE_SESSION="你複製的值"

python3 pawchive_client_v3.py favorites --type post      # 收藏的貼文
python3 pawchive_client_v3.py favorites --type artist    # 收藏的創作者
python3 pawchive_client_v3.py favpost fanbox 21971914 12323115
python3 pawchive_client_v3.py unfavpost fanbox 21971914 12323115
python3 pawchive_client_v3.py favcreator fanbox 21971914
python3 pawchive_client_v3.py unfavcreator fanbox 21971914
```

也可以用 `--session` 直接傳（會留在 shell history，較不建議）：

```bash
python3 pawchive_client_v3.py --session "你的值" favorites --type post
```

程式裡用：

```python
import os, pawchive_client_v3 as pc

s = os.environ["PAWCHIVE_SESSION"]
favs = pc.favorites_list(s, "post")
pc.favorite_post(s, "fanbox", "21971914", "12323115")
```

**session 會過期。** 過期後會拿到 `AuthError`，重新從瀏覽器複製一次即可。

---

## 7. 常見情境速查

**我想備份某個創作者的全部作品**
```bash
python3 pawchive_download.py fanbox 21971914 --max 9999 --dry-run   # 先看規模
python3 pawchive_download.py fanbox 21971914 --max 9999             # 再實際跑
```

**我只想要文字內容，不要圖**
```bash
python3 pawchive_download.py fanbox 21971914 --max 500 --metadata-only
```

**我想追蹤某創作者的新作**
先跑一次完整下載，之後定期重跑同一條指令——已有的會自動跳過，只抓新的。

**我想找特定主題的作品**
```bash
python3 pawchive_client_v3.py search "關鍵字" --max 200
```

**我從網頁看到一篇想存**
從網址抄出三個 ID，然後：
```bash
python3 pawchive_client_v3.py urls fanbox 21971914 12323115 | cut -f2 | wget -i -
```

**我想知道某個檔案是哪篇貼文的**
```bash
sha256sum 檔案.jpg                              # 算出雜湊
python3 pawchive_client_v3.py hash <雜湊值>     # 反查
```
（此索引不完整，查不到很常見。）

---

## 8. 疑難排解

**`錯誤: offset 必須是 50 的倍數（>= 0）`**
`--offset` 只能給 0、50、100、150…

**`錯誤: /posts 的 offset 硬上限是 50000（第 1000 頁）`**
全站貼文列表最多翻到第 1000 頁。要更深的資料請改用創作者貼文列表（那個沒有上限）。

**`錯誤: 找不到創作者 xxx`**
檢查 `service` 拼字（只有 `patreon` 和 `fanbox`）和 ID 是否正確。最保險的做法是從網頁網址複製。

**`AuthError: HTTP 401`**
session 沒帶或已過期。重新從瀏覽器複製 `session` cookie。

**`錯誤: 此操作需要 session`**
收藏功能忘了設 `PAWCHIVE_SESSION` 或 `--session`。

**下載的圖打不開 / 檔案大小是 0**
刪掉該檔重跑，續傳機制會重新下載。若持續失敗，可能是該檔案在站方已遺失。

**縮圖網址回 502**
`img.pawchive.pw` 的縮圖服務偶爾掛掉，屬正常。改用 `file.pawchive.pw` 的原始檔（`post_file_urls()` 給的就是原始檔）。
GUI 的預覽圖已經內建這個 fallback：先試縮圖，載不到才自動換原始檔，不用自己處理。

**GUI 打不開 / 連接埠被佔用**
換一個埠：`python3 pawchive_gui.py --port 9000`。若瀏覽器沒自動開啟，手動貼上終端機顯示的網址。

**GUI 裡的圖片全都是模糊的**
這不是故障，是右上角「模糊預覽」預設開啟。取消勾選即可恢復清晰，設定會被記住。
若想改成預設關閉，編輯 `pawchive_gui.py` 裡的 `const BLUR_DEFAULT = '1';`，把 `'1'` 改成 `'0'`。

**GUI 裡圖片破圖**
預覽圖是瀏覽器直連 `img.pawchive.pw`（縮圖），失敗才自動退回 `file.pawchive.pw` 的原始檔。
兩邊都載不到才會顯示「圖片載入失敗」，通常代表站方檔案已遺失。

**GUI 顯示 `forbidden: cross-origin request`**
這個本機伺服器沒有帳號密碼，所以會擋掉不是從它自己頁面發出的請求（避免其他網站偷偷叫它下載東西）。
正常從終端機顯示的網址開啟就不會遇到。若你用了自訂網域或反向代理指到它，就會被這道檢查擋下。

**下載很慢 / 想跑快一點**
先試 `--workers 6`：檔案下載走 CDN，並行不會加重 API 負擔，圖多的貼文效果最明顯。
`--delay 0` 可以取消 API 查詢的間隔，但這是沒有商業支援的鏡像站，建議保留預設的 0.5 秒。
目前實測沒有速率限制，但別把它當理所當然。

**下載中途按了 Ctrl+C**
會優雅停止，已下載的檔案會保留；重跑同一指令即可從中斷處續傳，不會重複抓已完成的部分。

**`ModuleNotFoundError: No module named 'pawchive_client_v3'`**
你的腳本要跟 `pawchive_client_v3.py` 放在同一個目錄，或在程式開頭加：
```python
import sys; sys.path.insert(0, "/home/user")
```

---

## 最後提醒

這個站聚合的是 Patreon / Fanbox 的付費內容，多數未經創作者授權。技術上 API 沒有任何限制，但**「抓得到」不等於「可以拿來用」**——尤其是再散布或商業用途，法律風險請自行評估。

如果某位創作者的作品你真的喜歡，直接去原站訂閱支持他們。
