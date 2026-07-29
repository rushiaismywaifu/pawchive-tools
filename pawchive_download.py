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
  - 檔名淨化，避免路徑穿越與非法字元
  - 失敗不中斷整批，最後統一列出
"""

import argparse
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


def safe_name(name, fallback="file"):
    """淨化檔名：去掉路徑分隔符與控制字元，避免寫到預期之外的位置。"""
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ") or fallback
    return name[:150]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def remote_size(url):
    """用 Range 請求探測檔案大小（此 CDN 支援 206 + Content-Range）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("Content-Range")
        if cr and "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        return int(r.headers.get("Content-Length") or 0)


def download(url, dest, max_bytes=None, retries=3):
    """
    下載單一檔案，支援續傳。
    回傳 "skip"（已完整） / "done" / "resume" / "toobig"
    """
    total = remote_size(url)
    if max_bytes and total > max_bytes:
        return "toobig", total

    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if total and have == total:
        return "skip", total
    if have > total:  # 本機檔案比遠端大，視為損毀，重下
        have = 0

    for attempt in range(retries):
        try:
            headers = {"User-Agent": UA}
            mode = "wb"
            if have:
                headers["Range"] = f"bytes={have}-"
                mode = "ab"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, mode) as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            return ("resume" if have else "done"), total
        except (urllib.error.URLError, OSError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
            have = os.path.getsize(dest) if os.path.exists(dest) else 0
    return "done", total


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
    a = ap.parse_args()

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

        files = pc.post_file_urls(post)
        if a.no_cover:
            cover = (post.get("file") or {}).get("path")
            files = [(n, u) for n, u in files if not (cover and cover in u)]

        for name, url in files:
            dest = os.path.join(pdir, safe_name(name, f"{pid}_file"))
            if a.dry_run:
                try:
                    sz = remote_size(url)
                    flag = "  [超過上限，會跳過]" if max_bytes and sz > max_bytes else ""
                    print(f"      {name}  ({human(sz)}){flag}")
                except Exception as e:
                    print(f"      {name}  (無法取得大小: {type(e).__name__})")
                continue
            try:
                status, size = download(url, dest, max_bytes)
                stats[status] += 1
                if status in ("done", "resume"):
                    stats["bytes"] += size
                mark = {"done": "✓", "resume": "↻", "skip": "·", "toobig": "✗"}[status]
                note = "  已存在" if status == "skip" else ("  超過上限跳過" if status == "toobig" else "")
                print(f"      {mark} {name}  ({human(size)}){note}")
            except Exception as e:
                stats["fail"] += 1
                failures.append((pid, name, f"{type(e).__name__}: {e}"))
                print(f"      ✗ {name}  失敗: {type(e).__name__}")

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
    main()
