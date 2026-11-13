#!/usr/bin/env python3
"""Fetch bilibili parts, audio and subtitles through the public API.

Why this exists: yt-dlp gets its metadata from the video *webpage*, and bilibili's
anti-bot returns HTTP 412 there while the JSON API keeps working (reproduced on
2026-09-26: `yt-dlp -f ba` -> 412 on every part, `x/web-interface/view` -> code 0).
So this is the fallback for that case, and it needs no cookies for public videos.

    python bili_fetch.py list  BV1xxxxxxxxx                 # what parts exist
    python bili_fetch.py audio BV1xxxxxxxxx -I 4-24         # download audio only
    python bili_fetch.py subs  BV1xxxxxxxxx -I 1-3          # turn subtitles into transcripts
    python bili_fetch.py resources BV1xxxxxxxxx             # find shared material links

Tier 1 of the subtitle/ASR ladder (`subs`) and the download step both live here.
`transcribe.py` then handles the audio -> text tiers.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://api.bilibili.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REFERER = "https://www.bilibili.com"
# 30280 = 192k, 30232 = 132k, 30216 = 64k; the API returns what anonymous access allows.
BEST_AUDIO = 30280


def _curl(url: str, headers: dict[str, str], timeout: int) -> bytes:
    cmd = ["curl", "-sS", "--max-time", str(timeout), "--compressed"]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"curl exited {proc.returncode}")
    return proc.stdout


def _get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    """GET with a curl fallback.

    bilibili's WAF answers HTTP 412 to Python's TLS fingerprint while answering 200 to
    `curl` for the byte-identical request -- reproduced 2026-09-26 by calling the same
    URL with the same headers from urllib, requests and curl within seconds of each
    other: urllib 412, requests 412, curl 200. It is not rate limiting: the same API
    keeps serving other clients. curl ships with Windows 10+ and every mainstream
    Linux/macOS, so retrying through it is cheaper than adding a TLS-impersonating
    dependency.
    """
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code != 412:
            raise
        if not shutil.which("curl"):
            raise SystemExit(
                "[error] bilibili answered 412 to this Python TLS fingerprint and curl is not\n"
                "        installed to retry with. Install curl (`where curl`), or wait -- sometimes\n"
                "        the block clears. Cookie login does NOT help with this one."
            )
        print("[info] 412 from Python client; retrying this request through curl", file=sys.stderr)
        return _curl(url, headers, timeout)


def api(path: str, params: dict, cookie: str | None = None) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    for attempt in range(1, 4):
        try:
            body = json.loads(_get(url, headers).decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"[error] {path} -> HTTP {e.code}")
        except urllib.error.URLError as e:
            raise SystemExit(f"[error] cannot reach bilibili: {e}")
    if body.get("code") != 0:
        raise SystemExit(f"[error] {path} -> code {body.get('code')}: {body.get('message')}")
    return body["data"]


def parse_bv(raw: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", raw)
    if not m:
        raise SystemExit(f"[error] no BV id found in {raw!r}")
    return m.group(1)


def parse_range(spec: str | None, parts: list[dict]) -> list[dict]:
    if not spec or spec == "all":
        return parts
    want: list[int] = []
    for chunk in spec.split(","):
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            want += list(range(int(a), int(b) + 1))
        else:
            want.append(int(chunk))
    picked = [p for p in parts if p["page"] in want]
    if not picked:
        raise SystemExit(f"[error] -I {spec} selected no parts")
    return picked


def stem_for(course: str, page: dict) -> str:
    """Match yt-dlp's naming so old and new downloads land side by side."""
    return f"p{page['page']:02d}_{course} p{page['page']:02d} {page['part']}"


def fetch_pages(bv: str, cookie: str | None) -> tuple[str, list[dict]]:
    data = api("/x/web-interface/view", {"bvid": bv}, cookie)
    course = re.sub(r"[\\/:*?\"<>|]", "_", data["title"]).strip()
    return course, data["pages"]


def cmd_list(args) -> int:
    bv = parse_bv(args.url)
    course, pages = fetch_pages(bv, args.cookie)
    total = sum(p["duration"] for p in pages)
    print(f"{course}  ({bv}, {len(pages)} 分P, {total // 3600}h{total % 3600 // 60:02d}m)\n")
    print(f"{'#':>3}  {'时长':>7}  {'cid':>12}  标题")
    for p in pages:
        d = p["duration"]
        print(f"{p['page']:>3}  {d // 60:>4}:{d % 60:02d}  {p['cid']:>12}  {p['part']}")
    return 0


def cmd_audio(args) -> int:
    bv = parse_bv(args.url)
    course, pages = fetch_pages(bv, args.cookie)
    picked = parse_range(args.items, pages)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Resolve stream URLs one request at a time (the API is what gets rate-limited), then
    # download them concurrently -- the media CDN is happy to serve several files at once
    # and it is the slow part (~120 KB/s per connection here).
    jobs: list[tuple[Path, list[str], int, dict]] = []
    for i, page in enumerate(picked, 1):
        target = outdir / f"{stem_for(course, page)}.m4a"
        if target.exists() and target.stat().st_size > 0 and not args.force:
            print(f"  skip (exists) {target.name}")
            continue
        if jobs and args.delay:
            time.sleep(args.delay)
        play = api("/x/player/playurl",
                   {"bvid": bv, "cid": page["cid"], "fnval": 16, "fourk": 1}, args.cookie)
        try:
            urls, want = expected_bytes(play, page["duration"])
        except LookupError:
            print(f"  [warn] {page['page']:>2} no audio stream returned, skipping", file=sys.stderr)
            continue
        jobs.append((target, urls, want, page))
        print(f"  queued {page['page']:>2} {page['part']}  ({page['duration'] // 60}m, ~{want / 1e6:.0f}MB, "
              f"{len(urls)} CDN host(s))")
    if not jobs:
        print("[info] nothing to download")
        return 0

    print(f"\ndownloading {len(jobs)} file(s) with {args.workers} worker(s)")
    failures: list[tuple[Path, dict]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, urls, target, args.cookie, want): (target, page)
                   for target, urls, want, page in jobs}
        for fut in as_completed(futures):
            target, page = futures[fut]
            try:
                fut.result()
                print(f"  ok   {target.name} ({target.stat().st_size / 1e6:.1f} MB)")
            except Exception as e:
                failures.append((target, page))
                print(f"  FAIL {target.name}: {e}", file=sys.stderr)

    # Stream URLs expire, so a fresh resolve can hand back a different, healthy node.
    if failures:
        print(f"\n[retry] {len(failures)} failed -- re-resolving stream URLs and trying again")
        retry_failures: list[tuple[Path, dict]] = []
        for i, (target, page) in enumerate(failures, 1):
            if i > 1 and args.delay:
                time.sleep(args.delay)
            play = api("/x/player/playurl", {"bvid": bv, "cid": page["cid"], "fnval": 16, "fourk": 1}, args.cookie)
            urls, want = expected_bytes(play, page["duration"])
            try:
                download(urls, target, args.cookie, want)
                print(f"  ok   {target.name} ({target.stat().st_size / 1e6:.1f} MB)")
            except Exception as e:
                retry_failures.append((target, page))
                print(f"  FAIL {target.name}: {e}", file=sys.stderr)
        if retry_failures:
            names = ", ".join(t.name[:3] for t, _ in retry_failures)
            print(f"\n[done] {len(jobs) - len(retry_failures)}/{len(jobs)} ok; still failing: {names}\n"
                  f"       re-run the same command to resume from the .part files", file=sys.stderr)
            return 1
    print(f"\n[done] {len(jobs)} file(s) -> {outdir}/")
    return 0


def cmd_verify(args) -> int:
    """Check local downloads. Decoding is the only trustworthy test: a corrupt DASH file
    still declares its full duration, and a parallel download produced 12 files of roughly
    the right size that ffmpeg could not decode."""
    bv = parse_bv(args.url)
    course, pages = fetch_pages(bv, args.cookie)
    picked = parse_range(args.items, pages)
    outdir = Path(args.outdir)
    if not shutil.which("ffmpeg"):
        print("[warn] ffmpeg not found -- falling back to a size check, which cannot detect\n"
              "       corruption inside a complete-looking file", file=sys.stderr)
    bad = []
    for i, page in enumerate(picked, 1):
        f = next(iter(outdir.glob(f"p{page['page']:02d}_*.m4a")), None)
        if not f:
            print(f"p{page['page']:02d}  MISSING")
            bad.append(page["page"])
            continue
        if i > 1 and args.delay:
            time.sleep(args.delay)
        play = api("/x/player/playurl", {"bvid": bv, "cid": page["cid"], "fnval": 16, "fourk": 1}, args.cookie)
        try:
            _, want = expected_bytes(play, page["duration"])
        except LookupError:
            want = 0
        got = f.stat().st_size
        ratio = got / want if want else 1.0
        errors = decode_errors(f)
        ok = ratio > 0.95 and errors == 0
        if not ok:
            bad.append(page["page"])
        print(f"p{page['page']:02d}  size {got / 1e6:6.1f}/{want / 1e6:6.1f} MB  ratio {ratio:.2f}  "
              f"decode errors {errors:>4}  {'OK' if ok else 'BAD'}")
    if bad:
        print(f"\n[bad] {len(bad)} file(s) unusable: {bad}\n"
              f"      delete them and re-run `audio` (sequential, verified)")
        return 1
    print(f"\n[ok] all {len(picked)} file(s) decode cleanly")
    return 0


def decode_errors(path: Path) -> int:
    """Count ffmpeg's decode complaints. Returns 0 when ffmpeg is unavailable."""
    if not shutil.which("ffmpeg"):
        return 0
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                       capture_output=True, text=True)
    return len([line for line in r.stderr.splitlines() if line.strip()])


def download(urls: list[str], target: Path, cookie: str | None, expected_bytes: int = 0,
             retries: int = 2, verify: bool = True) -> None:
    """Fetch one stream, trying every CDN host the API offered until one delivers it.

    Rules learned the hard way against bilibili's CDN (2026-09-26):

    * **Never resume by appending.** Fragmented DASH audio reassembled from a byte range
      still declares its full duration in the init segment, so a corrupt join *looks*
      complete -- ffprobe happily reports the whole length. Restart the file instead.
    * **Never trust the byte count.** Parallel downloads produced files of the correct
      size whose AAC frames were corrupt: 12 of 21 files failed to decode, one of them
      98.3% of the expected size. Sequential downloads of the same parts were clean, so
      the default is one connection at a time, and every file gets decoded before it is
      accepted.
    * **A bad CDN node truncates at a fixed offset.** Two parts failed at the same byte
      count on every retry of the same URL -- so retrying the URL is useless. The API
      returns `backupUrl` hosts for exactly this reason; each one gets a turn.
    """
    headers = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    part = target.with_suffix(target.suffix + ".part")
    failures = []
    for host, url in enumerate(urls, 1):
        for attempt in range(1, retries + 1):
            part.unlink(missing_ok=True)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    with part.open("wb") as fh:
                        while chunk := r.read(1 << 20):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                print(f"\r   {target.stem[:12]} host{host} "
                                      f"{done / 1e6:6.1f}/{total / 1e6:.1f} MB", end="", flush=True)
                want = max(expected_bytes, total)
                if want and done < want * 0.98:
                    raise IOError(f"short: {done / 1e6:.1f} of {want / 1e6:.1f} MB")
                if verify:
                    errors = decode_errors(part)
                    if errors:
                        raise IOError(f"corrupt: {errors} decode error(s)")
                part.replace(target)
                return
            except Exception as e:
                failures.append(f"host{host}: {e}")
                if attempt < retries:
                    print(f"\n   [warn] {target.stem[:12]} host{host}: {e}; retrying", file=sys.stderr)
                    time.sleep(2)
    part.unlink(missing_ok=True)
    raise SystemExit(f"all {len(urls)} CDN host(s) failed ({'; '.join(failures[:3])})")


def expected_bytes(play: dict, duration: int) -> tuple[list[str], int]:
    """Pick the best audio stream and predict its size from the declared bandwidth.

    Returns every CDN host the API offered (primary first) so a truncated node can be
    swapped out.
    """
    streams = (play.get("dash") or {}).get("audio") or []
    if streams:
        streams.sort(key=lambda s: (s.get("id") != BEST_AUDIO, -s.get("bandwidth", 0)))
        best = streams[0]
        urls = [best["baseUrl"], *(best.get("backupUrl") or [])]
        return urls, int(best.get("bandwidth", 0) / 8 * duration)
    durl = play.get("durl") or []  # some videos only expose a durl (mp4/flv) fallback
    if not durl:
        raise LookupError("no audio stream returned")
    return [durl[0]["url"], *(durl[0].get("backup_url") or [])], 0


def cmd_subs(args) -> int:
    """Tier 1: use the video's own subtitles instead of running ASR."""
    bv = parse_bv(args.url)
    course, pages = fetch_pages(bv, args.cookie)
    picked = parse_range(args.items, pages)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    got = 0
    for i, page in enumerate(picked, 1):
        if i > 1 and args.delay:
            time.sleep(args.delay)
        info = api("/x/player/v2", {"bvid": bv, "cid": page["cid"]}, args.cookie)
        subs = ((info.get("subtitle") or {}).get("subtitles")) or []
        if not subs:
            print(f"[{i}/{len(picked)}] {page['page']:>2} no subtitles")
            continue
        sub = next((s for s in subs if s.get("lan", "").startswith("zh")), subs[0])
        url = sub.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            body = json.loads(r.read().decode("utf-8"))
        lines = [(float(s["from"]), s["content"].strip()) for s in body.get("body", []) if s.get("content")]
        if not lines:
            print(f"[{i}/{len(picked)}] {page['page']:>2} subtitle track is empty")
            continue
        stem = stem_for(course, page)
        md = [f"# {stem}\n"]
        txt = []
        for start, text in lines:
            s = int(start)
            md.append(f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}] {text}")
            txt.append(text)
        (outdir / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        (outdir / f"{stem}.txt").write_text("\n".join(txt) + "\n", encoding="utf-8")
        print(f"[{i}/{len(picked)}] {page['page']:>2} {len(lines)} lines ({sub.get('lan_doc') or sub.get('lan')})")
        got += 1
    if not got:
        print("\n[none] no subtitles on the selected parts -- fall back to "
              "`transcribe.py` (cloud or local ASR)")
    return 0


# ------------------------------------------------------------------ resources

# Share-ID character class: `[A-Za-z0-9_-]`, never `\w` -- in Python `\w` matches CJK,
# so a pan link followed directly by Chinese text (".../s/1abc2def链接如下") would swallow
# the words into the match. Same reason the generic URL body excludes CJK, quotes and
# full-width brackets: PPT 导出的分享文本里链接后面经常直接跟中文或全角括号。
NETDISK_PATTERNS = (
    ("百度网盘", r"https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+"),
    ("蓝奏云", r"https?://[A-Za-z0-9.-]*lanzou[a-z]?\.com/[A-Za-z0-9_-]+"),
    ("夸克网盘", r"https?://pan\.quark\.cn/s/[A-Za-z0-9_-]+"),
    ("阿里云盘", r"https?://(?:www\.)?(?:aliyundrive|alipan)\.com/s/[A-Za-z0-9_-]+"),
    ("123云盘", r"https?://(?:www\.)?123pan\.(?:com|cn)/s/[A-Za-z0-9_-]+"),
    ("天翼云盘", r"https?://cloud\.189\.cn/[a-z]/[A-Za-z0-9]+"),
    ("微云", r"https?://share\.weiyun\.com/[A-Za-z0-9_-]+"),
)
URL_GENERIC = r"https?://[^\s\u4e00-\u9fff<>\"'（）)\]]+"
# Codes ride along in the URL (?pwd=) or in the surrounding text. 4-8 chars: 百度 4 位、
# 夸克/天翼可能更长。
CODE_PATTERNS = (
    r"[?&]pwd=([A-Za-z0-9]{4})",
    r"(?:提取码|密码|访问码)[:：=\s]*([A-Za-z0-9]{4,8})",
)


def classify(url: str) -> tuple[str, str] | None:
    """(类型, 标签) for one URL. None = not course material, skip it.

    B站站内视频链接（b23.tv、/video/BV）是"下一讲"，不是资料，跳过；站内专栏保留。
    """
    host_m = re.match(r"https?://([^/]+)", url)
    host = (host_m.group(1).lower() if host_m else "").removeprefix("www.")
    if host.endswith("b23.tv"):
        return None
    if host == "bilibili.com":
        if re.search(r"/video/|/festival/|/bangumi/|/cheese/", url):
            return None
        return "其他", "B站专栏"
    for label, pat in NETDISK_PATTERNS:
        if re.match(pat, url):
            return "网盘", label
    if re.search(r"\.(?:pdf|zip|rar|7z|docx?|pptx?|xlsx?)(?:[?#]|$)", url, re.I):
        return "直链文件", host
    if re.match(r"https?://(?:www\.)?(?:github|gitee)\.com/", url):
        return "代码仓库", host
    return "其他", host


def code_in_url(url: str) -> str | None:
    m = re.search(r"[?&]pwd=([A-Za-z0-9]{4})", url)
    return m.group(1) if m else None


def extract_resources(text: str) -> list[dict]:
    """Pull shareable course-material links out of one blob of text.

    One blob = the description or a single comment. Codes are paired only within the
    same blob (a code in a different comment almost certainly belongs to a different
    link); when a blob has several links, each gets the code nearest to it.
    """
    spans: list[tuple[int, int, str]] = []
    for _, pat in NETDISK_PATTERNS:
        for m in re.finditer(pat, text):
            spans.append((m.start(), m.end(), m.group(0)))
    for m in re.finditer(URL_GENERIC, text):
        if not any(s <= m.start() < e for s, e, _ in spans):
            spans.append((m.start(), m.end(), m.group(0)))
    codes = [(m.start(), m.group(1)) for pat in CODE_PATTERNS for m in re.finditer(pat, text)]

    out: list[dict] = []
    remaining = sorted(codes)  # (position, code)
    for start, _, url in sorted(spans):
        kind = classify(url)
        if not kind:
            continue
        typ, label = kind
        code = code_in_url(url)
        if not code and remaining:
            # Links and codes normally appear in reading order ("链接A ... 提取码：x
            # 链接B ... 密码：y"), so each link takes the nearest unused code to its
            # right; a code printed *before* the link still gets picked up by the
            # anywhere-fallback.
            right = [c for c in remaining if c[0] >= start]
            pool = right or remaining
            chosen = min(pool, key=lambda c: c[0] - start)
            remaining.remove(chosen)
            code = chosen[1]
        out.append({"type": typ, "label": label, "url": url, "code": code})
    return out


def fetch_file(url: str, target: Path, cookie: str | None) -> int:
    """Download one direct-link file (never a netdisk share). Returns size in bytes."""
    headers = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as r:
            with target.open("wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code != 412:
            raise SystemExit(f"[error] download failed: HTTP {e.code} ({url[:80]})")
        if not shutil.which("curl"):
            raise
        print("[info] 412 from Python client; retrying this download through curl", file=sys.stderr)
        cmd = ["curl", "-sSfL", "--max-time", "900"]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        subprocess.run(cmd + ["-o", str(target), url], check=True)
    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        raise SystemExit(f"[error] downloaded 0 bytes: {url[:80]}")
    return size


def collect_texts(args, bv: str) -> tuple[str, list[tuple[str, str]]]:
    """(course title, [(来源标注, 文本)]) from the description, pinned and hot comments."""
    data = api("/x/web-interface/view", {"bvid": bv}, args.cookie)
    texts = [("简介", data.get("desc") or "")]
    aid = data["aid"]

    # 置顶评论优先且单独标注；热评走新接口（旧 /x/v2/reply 的 pn 翻页已失效，只回第一页）。
    reply = api("/x/v2/reply/main", {"type": 1, "oid": aid, "mode": 3, "next": 0, "ps": 20},
                args.cookie)
    for r in reply.get("top_replies") or []:
        texts.append(("置顶评论", r["content"]["message"]))
    for r in reply.get("replies") or []:
        texts.append((f"热评(赞{r.get('like', 0)})", r["content"]["message"]))
    cursor = reply.get("cursor") or {}
    for page in range(2, args.pages + 1):
        if cursor.get("is_end") or not cursor.get("next"):
            break
        if page > 1 and args.delay:
            time.sleep(args.delay)
        reply = api("/x/v2/reply/main",
                    {"type": 1, "oid": aid, "mode": 3, "next": cursor["next"], "ps": 20},
                    args.cookie)
        for r in reply.get("replies") or []:
            texts.append((f"热评(赞{r.get('like', 0)})", r["content"]["message"]))
        cursor = reply.get("cursor") or {}
    return data.get("title") or bv, texts


def cmd_resources(args) -> int:
    """Find course-material links in the description and pinned/hot comments.

    Listing is read-only. --fetch downloads direct-link files only, after the user has
    agreed -- netdisk shares are printed with their codes for the user to open, because
    automating those means login flows, captchas and anti-abuse walls.
    """
    bv = parse_bv(args.url)
    outdir = Path(args.outdir)
    blobs: list[tuple[str, str]] = []

    title, texts = collect_texts(args, bv)
    blobs += texts

    if args.season:
        data = api("/x/web-interface/view", {"bvid": bv}, args.cookie)
        season = data.get("ugc_season") or {}
        episodes = [ep for sec in season.get("sections") or [] for ep in sec.get("episodes") or []]
        if not episodes:
            print("[warn] --season: this video does not belong to a collection", file=sys.stderr)
        for i, ep in enumerate([e for e in episodes if e.get("bvid") != bv], 1):
            if args.delay:
                time.sleep(args.delay)
            try:
                ep_data = api("/x/web-interface/view", {"bvid": ep["bvid"]}, args.cookie)
                blobs.append((f"合集·{ep_data.get('title') or ep.get('title', '')}",
                              ep_data.get("desc") or ""))
                ep_reply = api("/x/v2/reply/main",
                               {"type": 1, "oid": ep_data["aid"], "mode": 3, "next": 0, "ps": 20},
                               args.cookie)
                for r in ep_reply.get("top_replies") or []:
                    blobs.append((f"合集·{ep_data.get('title') or ''}置顶", r["content"]["message"]))
            except SystemExit as e:
                print(f"  [warn] 合集第 {i} 个分P 跳过: {e}", file=sys.stderr)
                continue
            print(f"\r  合集扫描 {i}/{len(episodes) - 1}", end="", flush=True)
    print()

    merged: dict[str, dict] = {}
    for source, text in blobs:
        for r in extract_resources(text):
            key = re.sub(r"^https?://", "", r["url"]).rstrip("/").lower()
            entry = merged.setdefault(key, {**r, "sources": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if not entry.get("code") and r.get("code"):
                entry["code"] = r["code"]
    if not merged:
        print("[none] 简介和评论里没有发现可提取的资料链接")
        return 0

    order = {"网盘": 0, "直链文件": 1, "代码仓库": 2, "其他": 3}
    rows = sorted(merged.values(), key=lambda r: (order.get(r["type"], 9), r["type"], r["url"]))
    print(f"{title}\n")
    for i, r in enumerate(rows, 1):
        code = f"  提取码: {r['code']}" if r["code"] else ""
        print(f"{i}. [{r['type']}·{r['label']}] {' / '.join(r['sources'])}{code}")
        print(f"   {r['url']}")

    outdir.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M")
    (outdir / "_resources.json").write_text(
        json.dumps({"bvid": bv, "title": title, "generated_at": now, "count": len(rows),
                    "resources": [{**r, "url_key": k} for k, r in merged.items()]},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# 配套资料 · {title}", "",
             f"> 来源：BV {bv} 的简介与评论区（{now}）。网盘链接需要你自己打开；",
             "> 分享可能随时失效，本清单只负责\"找到了\"。", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. **[{r['type']}·{r['label']}]** {' / '.join(r['sources'])}")
        lines.append(f"   <{r['url']}>")
        if r["code"]:
            lines.append(f"   提取码：`{r['code']}`")
        lines.append("")
    (outdir / "资料清单.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] {len(rows)} 条资料 -> {outdir / '资料清单.md'}")

    if args.fetch:
        direct = [r for r in rows if r["type"] == "直链文件"]
        if not direct:
            print("[info] --fetch: 没有可自动下载的直链文件；网盘链接请自行打开")
            return 0
        print(f"\n[fetch] 下载 {len(direct)} 个直链文件 -> {outdir / '资料'}/")
        for i, r in enumerate(direct, 1):
            name = urllib.parse.unquote(
                urllib.parse.urlparse(r["url"]).path.rsplit("/", 1)[-1]) or f"material_{i}"
            print(f"  [{i}/{len(direct)}] {name}")
            size = fetch_file(r["url"], outdir / "资料" / name, args.cookie)
            print(f"   {size / 1e6:.1f} MB")
        print(f"\n[done] 直链文件在 {outdir / '资料'}/，可用 materials.py 直接解析成课件笔记")
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("list", cmd_list, "list the parts"),
                               ("audio", cmd_audio, "download audio"),
                               ("verify", cmd_verify, "check local downloads for truncation"),
                               ("subs", cmd_subs, "save the video's own subtitles as transcripts"),
                               ("resources", cmd_resources,
                                "find shared course-material links in the description and pinned/hot comments")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("url", help="BV id, or any URL containing one")
        p.add_argument("--cookie", help="extra Cookie header, e.g. 'SESSDATA=...' (only needed for login-gated content)")
        p.add_argument("--delay", type=float, default=1.0,
                       help="seconds between API requests (default: 1; raise it if you start getting HTTP 412)")
        if name == "audio":
            p.add_argument("--workers", type=int, default=1,
                           help="parallel CDN downloads (default: 1). Higher values corrupted "
                                "the audio in testing -- bilibili's CDN returns bad frames when "
                                "pulled concurrently, and the file size still looks right.")
        if name == "resources":
            p.add_argument("--pages", type=int, default=1,
                           help="hot-comment pages to scan, 20 comments each (default: 1)")
            p.add_argument("--season", action="store_true",
                           help="also scan the description and pinned comment of every episode "
                                "in the collection (2 requests each)")
            p.add_argument("--fetch", action="store_true",
                           help="download direct-link files after listing (ask the user first). "
                                "Netdisk shares are never downloaded -- they are listed with "
                                "their codes for you to open")
            p.add_argument("-o", "--outdir", default="materials",
                           help="where 资料清单.md goes (default: materials)")
        elif name != "list":
            p.add_argument("-I", "--items", default="all", help="parts to fetch, e.g. 4-24 or 1,3,5 (default: all)")
            p.add_argument("-o", "--outdir", default="transcripts" if name == "subs" else "downloads")
            p.add_argument("--force", action="store_true", help="redo files that already exist")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
