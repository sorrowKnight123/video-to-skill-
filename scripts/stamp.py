#!/usr/bin/env python3
"""Turn lecture notes into reviewable notes: clickable timestamps, enforced, one HTML page.

Three subcommands, one job each:

    stamp.py link  讲义/ --course course.json          # `00:12:34` -> bilibili link
    stamp.py lint  讲义/                               # every bullet must carry a timestamp
    stamp.py html  讲义/ -o 复习页.html --course course.json

The point is the timestamps: a reviewer reads a claim, clicks `00:12:34`, and lands on
that second of the video. BiliLearn-AI does the same thing with an in-app player
(TimeLink.vue emits `jump` and the parent seeks), which only works inside their app --
a link works from any editor, any chat, any browser.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

# The six content sections our notes use. Prefix match so "易混与坑"/"易混与坑点" both hit.
SECTION_KEYS = ("概念", "公式", "推导", "例题", "易混", "考点")
# Only backticked HH:MM:SS is a citation. Bare times are durations in headers and must
# not be touched, so the backticks ARE the contract.
TS_RE = re.compile(r"`(\d{1,2}:\d{2}:\d{2})`")
TS_ANY_RE = re.compile(r"`(\d{1,2}:\d{2}:\d{2})`|\[(\d{1,2}:\d{2}:\d{2})\]\(https?://")
ITEM_RE = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")


# --------------------------------------------------------------------------- time

def parse_hms(value: str) -> int | None:
    """Seconds from HH:MM:SS / MM:SS / 754 / compact HHMMSS.

    Deliberately forgiving, borrowed from BiliLearn-AI's time.js: model output and手写笔记
    both drift, and a wrong-format timestamp is better normalised than dropped.
    """
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"\d{6}", value):  # HHMMSS
        return int(value[:2]) * 3600 + int(value[2:4]) * 60 + int(value[4:])
    if re.fullmatch(r"\d+", value):
        return int(value)
    parts = re.split(r"[:：.]", value)
    try:
        nums = [int(p) for p in parts if p != ""]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def bili_url(bv: str, part: int, seconds: int) -> str:
    return f"https://www.bilibili.com/video/{bv}?p={part}&t={seconds}"


# --------------------------------------------------------------------------- inputs

def part_of(path: Path) -> int | None:
    m = re.match(r"(\d{1,3})[-_ ]", path.name)
    return int(m.group(1)) if m else None


def load_course(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def note_files(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in targets:
        p = Path(raw)
        if p.is_dir():
            files += sorted(p.glob("*.md"))
        elif p.is_file():
            files.append(p)
    return files


def masked_lines(text: str) -> list[bool]:
    """Per line: True when it is inside a fenced code block (must not be rewritten)."""
    flags, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            flags.append(True)
            in_fence = not in_fence
            continue
        flags.append(in_fence)
    return flags


# --------------------------------------------------------------------------- link

def link_text(text: str, bv: str | None, part: int | None) -> tuple[str, int]:
    """Rewrite backticked timestamps into markdown links. Returns (text, count)."""
    if not bv or part is None:
        return text, 0
    n = 0
    out, skipped = [], masked_lines(text)
    for line, in_fence in zip(text.splitlines(), skipped):
        if in_fence or line.lstrip().startswith(("#", ">")) or "`" not in line:
            out.append(line)  # headings and the `> 来源：` line carry durations, not citations
            continue

        def repl(m: re.Match) -> str:
            nonlocal n
            secs = parse_hms(m.group(1))
            if secs is None:
                return m.group(0)
            n += 1
            return f"[{m.group(1)}]({bili_url(bv, part, secs)})"

        out.append(TS_RE.sub(repl, line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), n


def cmd_link(args) -> int:
    course = load_course(args.course)
    bv = (course or {}).get("bv")
    if not bv:
        print("[warn] 没有 course.json（--course），时间戳保持纯文本。先跑 "
              "`bili_fetch.py list <BV> --json -o course.json`", file=sys.stderr)
    total = files = 0
    for path in note_files(args.targets):
        part = part_of(path)
        text = path.read_text(encoding="utf-8")
        new, count = link_text(text, bv, part)
        if count:
            files += 1
            total += count
            if args.dry_run:
                changed = [
                    (a, b) for a, b in zip(text.splitlines(), new.splitlines()) if a != b
                ]
                print(f"{path.name}: {count} 处，示例：")
                for a, b in changed[:2]:
                    print(f"  - {a.strip()[:70]}")
                    print(f"  + {b.strip()[:70]}")
            else:
                path.write_text(new, encoding="utf-8")
        elif not args.dry_run:
            print(f"{path.name}: 无变化")
    verb = "将改" if args.dry_run else "已改"
    print(f"[{'dry-run' if args.dry_run else 'done'}] {verb} {total} 处时间戳，涉及 {files} 个文件")
    return 0


# --------------------------------------------------------------------------- lint

def cmd_lint(args) -> int:
    missing, checked = [], 0
    for path in note_files(args.targets):
        lines = path.read_text(encoding="utf-8").splitlines()
        section, fence = "", False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                fence = not fence
                continue
            if fence:
                continue
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            if not any(section.startswith(k) for k in SECTION_KEYS):
                continue
            m = ITEM_RE.match(line)
            if not m or line.lstrip().startswith(">"):
                continue
            if len(m.group(1)) > 0:  # nested item: its parent bullet owns the citation
                continue
            checked += 1
            block = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or ITEM_RE.match(nxt) or nxt.lstrip().startswith(("#", "```", "|")):
                    break
                block.append(nxt)
                j += 1
            if not any(TS_ANY_RE.search(b) for b in block):
                body = m.group(3).strip()
                missing.append(f"{path.name}:{i + 1}  {body[:34]}")
    print(f"[lint] 检查 {checked} 个条目，缺时间戳 {len(missing)} 个")
    for row in missing:
        print(f"  {row}")
    if missing and not args.warn:
        print("\n每个条目都必须带 `HH:MM:SS`（取自转录）。归纳性内容请写成自然段，"
              "段落豁免。", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- html

CSS = """
:root{--bg:#faf7f0;--fg:#26241f;--muted:#8b8478;--line:#e6e1d3;--accent:#9a3324;
--accent-soft:#f1e3de;--code:#f2efe6;--serif:Georgia,"Times New Roman","Adobe 宋体 Std L",
"华文中宋","宋体",serif;--sans:"Segoe UI","Microsoft YaHei UI","Noto Sans SC",sans-serif;
--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#191713;--fg:#e8e4d9;--muted:#95908a;
--line:#33302a;--accent:#d9927f;--accent-soft:#3a241d;--code:#211f1b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:var(--serif);font-size:17px;line-height:1.95;letter-spacing:.015em}
.layout{display:flex;align-items:flex-start}
.side{position:sticky;top:0;width:250px;height:100vh;overflow:auto;padding:30px 20px;
border-right:1px solid var(--line);font-family:var(--sans);font-size:12.5px;
background:#f4f0e5}
@media (prefers-color-scheme:dark){.side{background:#1d1a16}}
.side h1{font-family:var(--serif);font-size:15px;line-height:1.5;margin:0 0 4px;color:var(--fg)}
.side .meta{color:var(--muted);font-size:11.5px;margin-bottom:20px;font-family:var(--sans)}
.side a{display:block;color:var(--muted);text-decoration:none;padding:2.5px 8px;border-radius:4px}
.side a:hover{color:var(--accent);background:var(--accent-soft)}
.side a.lec{color:var(--fg);font-weight:600;margin:10px 0 2px}
.main{flex:1;min-width:0;max-width:46rem;margin:0 auto;padding:64px 44px 90px}
h1{font-family:"华文中宋","Adobe 宋体 Std L",var(--serif);font-size:2rem;font-weight:400;
line-height:1.45;margin:0 0 12px}
strong{font-weight:600}
h2{font-family:var(--sans);font-size:13.5px;font-weight:600;letter-spacing:.18em;
color:var(--accent);margin:3.4em 0 1.4em;padding-bottom:9px;border-bottom:1px solid var(--line)}
h3{font-family:var(--sans);font-size:12.5px;font-weight:600;letter-spacing:.1em;
color:var(--muted);margin:2.2em 0 .9em}
p{margin:1.15em 0}
ul,ol{padding-left:1.5em;margin:1em 0 1.3em}
li{margin:.72em 0}
li>ul,li>ol{margin:.45em 0}
li::marker{color:var(--muted)}
a{color:var(--accent)}
a.ts{display:inline-block;padding:0 2px;margin:0 1px;color:var(--accent);opacity:.78;
font-size:.76em;font-family:var(--mono);line-height:1.5;text-decoration:none}
a.ts:hover{opacity:1;background:var(--accent-soft);border-radius:3px}
code{background:var(--code);padding:1px 6px;border-radius:3px;font-size:.82em;
font-family:var(--mono)}
.src{color:var(--muted);font-size:13px;line-height:1.75;margin:0 0 30px;padding:0 0 4px 14px;
border-left:2px solid var(--line);font-family:var(--sans)}
section.sec-考点 h2{color:var(--warn);border-bottom-color:var(--warn)}
section.sec-考点 h2::after{content:"考点";font-size:10px;margin-left:10px;padding:1px 7px;
border-radius:3px;background:var(--warn-soft);color:var(--warn);letter-spacing:.2em}
.foot{margin-top:64px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);
font-size:12px;font-family:var(--sans)}
hr{border:0;border-top:1px solid var(--line);margin:2.6em 0}
@media (max-width:900px){.layout{display:block}.side{position:static;width:auto;height:auto;
border-right:0;border-bottom:1px solid var(--line)}.main{padding:34px 22px 60px}}
@media print{.side{display:none}.main{max-width:none;padding:0}
a.ts{color:#000;border:0;padding:0}}
"""


def inline_md(text: str) -> str:
    """Tiny inline renderer: code spans, bold, links, timestamp chips."""
    text = html.escape(text, quote=False)
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{part[1:-1]}</code>")
            continue
        part = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part)
        part = re.sub(
            r"\[([^\]]+)\]\((https?://[^)]+)\)",
            lambda m: f'<a class="ts" href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'
            if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", m.group(1))
            else f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
            part,
        )
        out.append(part)
    return "".join(out)


def render_file(path: Path) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (lecture_title, body_html, [(section_name, anchor_id)])."""
    lines = path.read_text(encoding="utf-8").splitlines()
    title = path.stem
    body: list[str] = []
    toc: list[tuple[str, str]] = []
    list_stack: list[tuple[int, str]] = []
    in_table, table_rows = False, []
    para: list[str] = []
    section_open = False
    item: list[str] = []  # text of the <li> being built, including its continuation lines
    quote: list[str] = []  # consecutive '> ' lines form one paragraph
    part = part_of(path) or 0

    def flush_item() -> None:
        if item:
            body.append("<li>" + inline_md(" ".join(item)) + "</li>")
            item.clear()

    def flush_quote() -> None:
        if quote:
            body.append(f'<p class="src">{inline_md(" ".join(quote))}</p>')
            quote.clear()

    def close_lists() -> None:
        flush_item()
        while list_stack:
            body.append(f"</{list_stack.pop()[1]}>")

    def flush_para() -> None:
        if para:
            body.append(f"<p>{inline_md(' '.join(para))}</p>")
            para.clear()

    def flush_table() -> None:
        nonlocal in_table
        if not table_rows:
            return
        rows = [r for r in table_rows if not re.fullmatch(r"[\s|:\-]+", r)]
        cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
        if not cells:
            table_rows.clear()
            in_table = False
            return
        head, rest = cells[0], cells[1:]
        body.append("<table><thead><tr>" + "".join(f"<th>{inline_md(c)}</th>" for c in head) + "</tr></thead><tbody>")
        for row in rest:
            body.append("<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in row) + "</tr>")
        body.append("</tbody></table>")
        table_rows.clear()
        in_table = False

    for line in lines:
        if not line.startswith("> "):
            flush_quote()
        if line.startswith("```"):
            flush_para()
            close_lists()
            continue
        if line.startswith("# "):
            title = line[2:].strip()
            body.append(f"<h1>{inline_md(title)}</h1>")
            continue
        if line.startswith("> "):
            flush_para()
            close_lists()
            quote.append(line[2:].strip())
            continue
        if line.startswith("## "):
            flush_para()
            close_lists()
            if section_open:
                body.append("</section>")
            name = line[3:].strip()
            anchor = f"p{part:02d}-{re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', name)[:12]}"
            toc.append((name, anchor))
            cls = "sec-考点" if name.startswith("考点") else "sec"
            body.append(f'<section class="{cls}" id="{anchor}"><h2>{inline_md(name)}</h2>')
            section_open = True
            continue
        if line.startswith("### "):
            flush_para()
            close_lists()
            body.append(f"<h3>{inline_md(line[4:].strip())}</h3>")
            continue
        if line.lstrip().startswith("|"):
            flush_para()
            close_lists()
            in_table = True
            table_rows.append(line)
            continue
        if in_table:
            flush_table()
        m = ITEM_RE.match(line)
        if m:
            flush_para()
            indent = len(m.group(1))
            kind = "ol" if m.group(2)[0].isdigit() else "ul"
            while list_stack and indent < list_stack[-1][0]:
                flush_item()
                body.append(f"</{list_stack.pop()[1]}>")
            flush_item()
            if not list_stack or indent > list_stack[-1][0]:
                body.append(f"<{kind}>")
                list_stack.append((indent, kind))
            elif list_stack[-1][1] != kind:
                # same level, different marker: close and reopen so 1./2. keep numbers
                body.append(f"</{list_stack.pop()[1]}>")
                body.append(f"<{kind}>")
                list_stack.append((indent, kind))
            item.append(m.group(3).strip())
            continue
        if list_stack and line[:1] in (" ", "	") and line.strip():
            item.append(line.strip())  # continuation of the bullet above
            continue
        if not line.strip():
            flush_para()
            close_lists()
            continue
        para.append(line.strip())

    flush_quote()
    flush_para()
    flush_table()
    close_lists()
    if section_open:
        body.append("</section>")
    return title, "\n".join(body), toc


def cmd_html(args) -> int:
    course = load_course(args.course) or {}
    files = note_files(args.targets)
    entries = []
    for path in files:
        part = part_of(path)
        if part is None:
            continue
        title, body, toc = render_file(path)
        entries.append((part, path, title, body, toc))
    if not entries:
        print("[error] 没找到讲义（文件名需要 NN- 前缀）", file=sys.stderr)
        return 1
    entries.sort(key=lambda e: e[0])

    nav = [f'<div class="meta">共 {len(entries)} 讲 · 点时间戳回原视频</div>']
    for part, path, title, _body, toc in entries:
        label = f"{part:02d} {title.split('讲 ', 1)[-1] if '讲 ' in title else title}"
        nav.append(f'<a class="lec" href="#p{part:02d}">{html.escape(label)}</a>')

    body = []
    for part, path, title, content, _toc in entries:
        body.append(f'<article id="p{part:02d}">{content}</article>')
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(course.get('title') or '课程复习笔记')}</title>
<style>{CSS}</style></head>
<body><div class="layout">
<nav class="side"><h1>{html.escape(course.get('title') or '课程复习笔记')}</h1>
{''.join(nav)}</nav>
<main class="main">
{''.join(body)}
<p class="foot">时间戳链接会打开 B 站并跳到对应秒数（需联网）。本页本身离线可读，
无外部依赖。公式以 Unicode 文本呈现。</p>
</main></div></body></html>
"""
    out = Path(args.output)
    out.write_text(doc, encoding="utf-8")
    size = out.stat().st_size / 1024
    print(f"[done] {out}  {len(entries)} 讲  {size:.0f} KB")
    return 0


# --------------------------------------------------------------------------- main

def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("link", help="把反引号时间戳改写成 B 站链接")
    p.add_argument("targets", nargs="+")
    p.add_argument("--course", help="course.json（含 bv/pages），由 bili_fetch.py list --json 生成")
    p.add_argument("--dry-run", action="store_true", help="只显示将改动，不写回")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("lint", help="检查每个条目是否带时间戳")
    p.add_argument("targets", nargs="+")
    p.add_argument("--warn", action="store_true", help="只报告，不返回非零退出码")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("html", help="生成单页自包含复习笔记")
    p.add_argument("targets", nargs="+")
    p.add_argument("-o", "--output", default="复习页.html")
    p.add_argument("--course", help="course.json（含 bv/title）")
    p.set_defaults(func=cmd_html)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
