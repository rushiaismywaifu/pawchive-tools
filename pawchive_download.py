#!/usr/bin/env python3
"""
Pawchive 批次下載器 — 把某位創作者的貼文與附件抓到本機。

依賴 pawchive_client_v3.py（同目錄），純標準函式庫。

用法:
    # 先看看會下載什麼，不實際寫檔（強烈建議先跑這個）
    python3 pawchive_download.py fanbox 21971914 --max 10 --dry-run

    # 實際下載最新 10 篇的附件
    python3 pawchive_download.py fanbox 21971914 --max 10

    # 指定輸出目錄、跳過封面只抓附件、限制檔案大小
    python3 pawchive_download.py fanbox 21971914 --max 50 --out ./dl --no-cover --max-mb 50

    # 只存貼文內文（JSON），不下載任何檔案
    python3 pawchive_download.py fanbox 21971914 --max 100 --metadata-only

特性:
  - 斷點續傳：已存在且大小相符的檔案自動跳過（file CDN 支援 Range，中斷的檔案會續傳）
  - 每篇貼文一個資料夾，內含 post.json（完整內文）與所有檔案
  - 檔名淨化（含長度按位元組截斷），避免路徑穿越、非法字元與 ENAMETOOLONG
  - 同一篇貼文內的檔案並行下載（--workers，預設 3），API 查詢仍照 --delay 循序
  - 封面與附件指向同一個檔案時只抓一次（實測約六成貼文是這種狀況）
  - 失敗不中斷整批，最後統一列出
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pawchive_client_v3 as pc

UA = pc.UA
RETRY_CODES = pc._RETRY_CODES
NAME_MAX_BYTES = 255   # ext4 / APFS 的單一檔名上限，單位是「位元組」不是字元


def safe_name(name, fallback="file", max_bytes=NAME_MAX_BYTES):
    """
    淨化檔名：去掉路徑分隔符與控制字元，避免寫到預期之外的位置。

    長度以「位元組」計算：檔案系統的 255 上限算的是 bytes，一個中日文字在
    UTF-8 佔 3 bytes，本站又多是日文檔名——照字元數截到 150 會直接吃 ENAMETOOLONG。
    需要截斷時保留副檔名，否則存回來的檔案認不出型別。
    """
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ") or fallback
    if len(name.encode("utf-8")) <= max_bytes:
        return name

    stem, ext = os.path.splitext(name)
    ext_b = ext.encode("utf-8")
    if len(ext_b) > 16:          # 不像副檔名（標題裡剛好有個點），整串當主檔名處理
        stem, ext_b = name, b""
    budget = max(1, max_bytes - len(ext_b))
    # 先截 bytes 再用 ignore 解碼，尾巴被切一半的多位元組字元會被丟掉，不會留下亂碼
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").strip(". ")
    return (stem or fallback) + ext_b.decode("utf-8", "ignore")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def remote_size(url, retries=3):
    """用 Range 請求探測檔案大小（此 CDN 支援 206 + Content-Range）。大小不明回 0。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    tail = cr.rsplit("/", 1)[1].strip()
                    if tail.isdigit():
                        return int(tail)
                if r.status == 206:
                    # 吃了 Range 卻沒給總長度（Content-Range: bytes 0-0/*）。
                    # 此時 Content-Length 是那 1 個 byte，拿來當檔案大小會大錯特錯。
                    return 0
                return int(r.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(pc._retry_delay(attempt))
                continue
            raise
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(pc._retry_delay(attempt))
    return 0


def download(url, dest, max_bytes=None, retries=3):
    """
    下載單一檔案，支援續傳。
    回傳 (status, size)，status 為 "skip"（已完整）/ "done" / "resume" / "toobig"。
    """
    total = remote_size(url)
    if max_bytes and total > max_bytes:
        return "toobig", total

    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if total and have == total:
        return "skip", total
    if have > total:  # 本機檔案比遠端大（或遠端大小不明），視為損毀，重下
        have = 0

    for attempt in range(retries):
        try:
            headers = {"User-Agent": UA}
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                # 帶了 Range 但伺服器回 200 = 它不吃續傳，body 是「整個檔案」。
                # 這種時候還用 append 會把前半段接在舊資料後面，檔案直接毀掉，
                # 所以只有確認拿到 206 才續寫，否則一律覆寫重來。
                resumed = bool(have) and r.status == 206
                with open(dest, "ab" if resumed else "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            got = os.path.getsize(dest)
            if total and got != total:
                # 連線中途斷掉不一定會拋例外，不核對大小會把半截檔案當成功
                raise OSError(f"下載不完整：{got}/{total} bytes")
            return ("resume" if resumed else "done"), (total or got)
        except urllib.error.HTTPError as e:
            # HTTPError 也是 OSError 子類，先攔下來：404 / 403 重試三次只是白等
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(pc._retry_delay(attempt))
                have = os.path.getsize(dest) if os.path.exists(dest) else 0
                continue
            raise
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(pc._retry_delay(attempt))
            have = os.path.getsize(dest) if os.path.exists(dest) else 0
    return "done", total


def download_post_files(files, pdir, pid, max_bytes=None, workers=3, should_stop=None):
    """
    下載一篇貼文的所有檔案，回傳與輸入同順序的 (檔名, status, size, err)。

    - files：post_file_urls() 的輸出
    - workers：檔案走 file CDN（和 API 不同主機），並行不會壓到 API 的流量控制
    - should_stop：回傳 True 時停掉還沒開始的檔案（GUI 的「取消」用），
      已中止的檔案不會出現在回傳結果裡
    - 同名不同檔會自動補 _1 / _2 後綴；不這麼做時後面的檔案會蓋掉前面的，
      並行下載更會變成兩條執行緒寫同一個檔案
    """
    jobs, used = [], set()
    for name, url in files:
        base = safe_name(name, f"{pid}_file")
        cand, n = base, 1
        while cand.lower() in used:
            stem, ext = os.path.splitext(base)
            cand, n = f"{stem}_{n}{ext}", n + 1
        used.add(cand.lower())
        jobs.append((name, url, os.path.join(pdir, cand)))

    def one(job):
        name, url, dest = job
        if should_stop and should_stop():
            return None
        try:
            status, size = download(url, dest, max_bytes)
            return name, status, size, None
        except Exception as e:
            return name, "fail", 0, f"{type(e).__name__}: {e}"

    if workers <= 1 or len(jobs) <= 1:
        out = []
        for job in jobs:
            r = one(job)
            if r is None:
                break
            out.append(r)
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return [r for r in ex.map(one, jobs) if r is not None]


def _probe_sizes(files, workers=3):
    """dry-run 用：並行探測檔案大小，回傳與輸入同順序的 (檔名, size, 錯誤型別或 None)。"""
    def one(item):
        name, url = item
        try:
            return name, remote_size(url), None
        except Exception as e:
            return name, 0, type(e).__name__

    if workers <= 1 or len(files) <= 1:
        return [one(f) for f in files]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, files))


def main():
    ap = argparse.ArgumentParser(description="Pawchive 批次下載器")
    ap.add_argument("service", help="patreon 或 fanbox")
    ap.add_argument("creator_id")
    ap.add_argument("--max", type=int, default=20, help="最多處理幾篇貼文（預設 20）")
    ap.add_argument("--out", default="downloads", help="輸出目錄（預設 downloads）")
    ap.add_argument("--dry-run", action="store_true", help="只列出將下載的檔案，不寫檔")
    ap.add_argument("--no-cover", action="store_true", help="跳過封面圖，只抓附件")
    ap.add_argument("--metadata-only", action="store_true", help="只存 post.json，不下載檔案")
    ap.add_argument("--max-mb", type=float, default=0, help="單檔大小上限（MB），0 = 不限")
    ap.add_argument("--delay", type=float, default=0.5, help="每篇貼文之間的間隔秒數")
    ap.add_argument("--workers", type=int, default=3,
                    help="同一篇貼文內同時下載幾個檔案（預設 3，設 1 為循序）；"
                         "檔案走 CDN，API 查詢仍照 --delay 循序進行")
    a = ap.parse_args()
    a.workers = max(1, a.workers)

    max_bytes = int(a.max_mb * 1024 * 1024) if a.max_mb else None

    try:
        prof = pc.creator_profile(a.service, a.creator_id)
    except pc.NotFoundError:
        raise SystemExit(f"找不到創作者 {a.service}/{a.creator_id}")
    except pc.PawchiveError as e:
        raise SystemExit(f"錯誤: {e}")

    cname = safe_name(prof.get("name"), a.creator_id)
    root = os.path.join(a.out, f"{a.service}_{cname}_{a.creator_id}")
    print(f"創作者：{prof.get('name')} ({a.service}/{a.creator_id})")
    print(f"輸出至：{root}{'   [DRY RUN 不寫檔]' if a.dry_run else ''}\n")

    posts = pc.paginate(
        lambda o: pc.creator_posts(a.service, a.creator_id, o),
        max_items=a.max, delay=a.delay,
    )
    print(f"取得 {len(posts)} 篇貼文\n")

    stats = {"done": 0, "resume": 0, "skip": 0, "toobig": 0, "fail": 0, "bytes": 0}
    failures = []

    for i, post in enumerate(posts, 1):
        pid = post.get("id")
        date = (post.get("published") or "")[:10]
        title = post.get("title") or ""
        pdir = os.path.join(root, safe_name(f"{date}_{pid}_{title[:40]}", pid))
        print(f"[{i}/{len(posts)}] {date}  {title[:50]}")

        if not a.dry_run:
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, "post.json"), "w", encoding="utf-8") as f:
                json.dump(post, f, ensure_ascii=False, indent=2)

        if a.metadata_only:
            continue

        files = pc.post_file_urls(post, include_cover=not a.no_cover)

        if a.dry_run:
            for name, sz, err in _probe_sizes(files, a.workers):
                if err:
                    print(f"      {name}  (無法取得大小: {err})")
                else:
                    flag = "  [超過上限，會跳過]" if max_bytes and sz > max_bytes else ""
                    print(f"      {name}  ({human(sz)}){flag}")
            time.sleep(a.delay)
            continue

        for name, status, size, err in download_post_files(
                files, pdir, pid, max_bytes, a.workers):
            if err:
                stats["fail"] += 1
                failures.append((pid, name, err))
                print(f"      ✗ {name}  失敗: {err.split(':')[0]}")
                continue
            stats[status] += 1
            if status in ("done", "resume"):
                stats["bytes"] += size
            mark = {"done": "✓", "resume": "↻", "skip": "·", "toobig": "✗"}[status]
            note = "  已存在" if status == "skip" else ("  超過上限跳過" if status == "toobig" else "")
            print(f"      {mark} {name}  ({human(size)}){note}")

        time.sleep(a.delay)

    print("\n" + "─" * 50)
    if a.dry_run:
        print("DRY RUN 結束，未寫入任何檔案。移除 --dry-run 即可實際下載。")
    else:
        print(f"完成：新增 {stats['done']}、續傳 {stats['resume']}、跳過 {stats['skip']}、"
              f"超過上限 {stats['toobig']}、失敗 {stats['fail']}")
        print(f"本次下載量：{human(stats['bytes'])}")
        if failures:
            print("\n失敗清單：")
            for pid, name, err in failures:
                print(f"  {pid}  {name}  {err}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # 斷點續傳是內建的：中斷只會留下半截檔案，重跑同一指令就能接續。
        # 沒這層攔截時 Ctrl+C 會噴一大段 traceback，看起來像壞掉。
        print("\n已中斷（Ctrl+C）。已下載與寫入的檔案會保留，重跑同一指令即可續傳。")
