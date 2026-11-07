#!/usr/bin/env python3
"""Fetch bilibili parts, audio and subtitles through the public API.

Why this exists: yt-dlp gets its metadata from the video *webpage*, and bilibili's
anti-bot returns HTTP 412 there while the JSON API keeps working (reproduced on
2026-09-26: `yt-dlp -f ba` -> 412 on every part, `x/web-interface/view` -> code 0).
So this is the fallback for that case, and it needs no cookies for public videos.

    python bili_fetch.py list  BV1xxxxxxxxx                 # what parts exist
    python bili_fetch.py audio BV1xxxxxxxxx -I 4-24         # download audio only
    python bili_fetch.py subs  BV1xxxxxxxxx -I 1-3          # turn subtitles into transcripts

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

# Windows rejects these characters in a file name; macOS/Linux reject "/" only. Course
# titles were already sanitised but part titles were not, so a part called "1: 导论"
# crashed the download when it tried to create the file.
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def safe_filename_part(text: str) -> str:
    """Turn a title into something all three desktop OSes accept as a file name."""
    return INVALID_FILENAME_CHARS.sub("_", str(text)).strip().rstrip(".")


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
    return (f"p{page['page']:02d}_{safe_filename_part(course)}"
            f" p{page['page']:02d} {safe_filename_part(page['part'])}")


def fetch_pages(bv: str, cookie: str | None) -> tuple[str, list[dict]]:
    data = api("/x/web-interface/view", {"bvid": bv}, cookie)
    course = safe_filename_part(data["title"])
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


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("list", cmd_list, "list the parts"),
                               ("audio", cmd_audio, "download audio"),
                               ("verify", cmd_verify, "check local downloads for truncation"),
                               ("subs", cmd_subs, "save the video's own subtitles as transcripts")):
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
        if name != "list":
            p.add_argument("-I", "--items", default="all", help="parts to fetch, e.g. 4-24 or 1,3,5 (default: all)")
            p.add_argument("-o", "--outdir", default="transcripts" if name == "subs" else "downloads")
            p.add_argument("--force", action="store_true", help="redo files that already exist")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
