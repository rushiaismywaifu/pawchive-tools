# Pawchive Tools

> **Pawchive API v1 完整生態工具集**：包含 OpenAPI 規格、Python 客戶端 (CLI / Library)、創作者批次下載器與本地 Web GUI 瀏覽器。

本儲存庫提供了對 [Pawchive.pw](https://pawchive.pw) 存檔服務（支援 Fanbox, Patreon, Fantia 等二次元創作者內容）的全套本機工具與技術指南。

---

## 📦 專案檔案清單與說明

| 檔案名稱 | 類型 | 功能說明 |
| :--- | :--- | :--- |
| **[`pawchive_client_v3.py`](./pawchive_client_v3.py)** | 核心客戶端 | 支援 CLI 命令列與 Python 模組匯入，實作完整例外繼承階層 (`PawchiveError` / `AuthError` / `ConflictError`)、自動分頁查詢、gzip 自動解壓與 CDN 原圖／縮圖連結解析。 |
| **[`pawchive_download.py`](./pawchive_download.py)** | 批次下載器 | 針對創作者貼文與附件的進階下載工具，支援斷點續傳 (`Range` Header)、同篇貼文並行下載 (`--workers`)、重複封面自動去重、延遲流量控制、dry-run 試跑與 `--metadata-only` 模式。 |
| **[`pawchive_gui.py`](./pawchive_gui.py)** | Web GUI 瀏覽器 | 零依賴的本機瀏覽器介面 (`http://127.0.0.1:8765`)。以本地後端代理解決 API CORS 問題，預覽圖直連縮圖 CDN（失敗自動退回原始檔）；內建創作者搜尋、貼文詳情檢視、背景下載卡片與**模糊預覽 (`BLUR_DEFAULT`)** 隱私保護開關，並以 Host / Origin 檢查擋下跨站請求。 |
| **[`pawchive_openapi.json`](./pawchive_openapi.json)** | API 規格檔 | 自官方 Swagger UI 抽取的完整 OpenAPI 3.0 JSON 規格文件（涵蓋全站 19 個 RESTful API 端點）。 |
| **[`pawchive_api_guide.md`](./pawchive_api_guide.md)** | API 參考手冊 | 詳解 Base URL、非 CORS 標頭特性、`file.` / `img.` CDN 子網域規定、認證機制與各端點欄位定義。 |
| **[`pawchive_教學.md`](./pawchive_教學.md)** | 完整使用手冊 | 全方位上手教學，包含 CLI 使用實例、`curl` 請求範例、Python Code Snippets、GUI 啟動指南與疑難排解（如模糊預覽設定記憶）。 |
| **[`test_v3_mock.py`](./test_v3_mock.py)** | 測試腳本 | 以本機 mock server 跑的 `unittest` 測試（48 項，完全不打正式站）：重試與錯誤映射、gzip、分頁、檔名淨化、斷點續傳與並行下載、創作者搜尋索引。 |

---

## 🚀 快速開始

### 1. 啟動 Web GUI 圖形化瀏覽器
```bash
python3 pawchive_gui.py
```
啟動後會自動用系統瀏覽器開啟 `http://127.0.0.1:8765`。
* 提示：右上角有**模糊預覽**勾選框，預設開啟以保護公共場合瀏覽隱私；取消勾選會記憶在 `localStorage`，重新整理也不會跑掉。

### 2. 使用命令列 CLI 查詢貼文
```bash
# 查詢最新貼文
python3 pawchive_client_v3.py recent

# 搜尋特定關鍵字
python3 pawchive_client_v3.py search "genshin" --max 100

# 查看創作者資料與所有下載連結
python3 pawchive_client_v3.py profile fanbox 21971914
python3 pawchive_client_v3.py urls fanbox 21971914 12323115
```

### 3. 批次下載創作者內容
```bash
# 先以 dry-run 查看會下載哪些內容與檔案數
python3 pawchive_download.py fanbox 21971914 --max 10 --dry-run

# 實際開始下載（自動斷點續傳、同篇貼文並行抓檔）
python3 pawchive_download.py fanbox 21971914 --max 50 --out ./downloads --workers 6
```

### 4. 執行測試
```bash
python3 test_v3_mock.py       # 全部跑在本機 mock server 上，不會連到正式站
```

---

## 📖 閱讀詳細文檔

- **想了解工具完整用法、常見教學與疑難排解**：請閱讀 [`pawchive_教學.md`](./pawchive_教學.md)
- **想深入探索後端 API 端點、CORS 與 CDN 架構**：請閱讀 [`pawchive_api_guide.md`](./pawchive_api_guide.md)
