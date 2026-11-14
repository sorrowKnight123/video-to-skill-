#!/usr/bin/env python3
"""Pull reusable figures out of courseware into the knowledge base's images/ directory.

The rule from the project owner: notes get *courseware images*, never model-drawn ones.
So this script only extracts what the courseware already contains, per format:

    PDF    whole-page render for pages that carry layout a text dump loses
           (diagrams -> bitmap coverage, formulas -> vector paths); plain-text pages
           are skipped, their text is already in the notes via materials.py
    PPTX   unpack ppt/media, mapped per slide through each slide's .rels
    DOCX   unpack word/media in document order
    images collected as-is

Everything else is the honest empty case: a text-only docx yields 0 images, the index
says so, and the notes simply don't get illustrations.

    python illustrate.py <课件目录> -o <知识库>/references/images [-n <课件笔记目录>] [--all]
"""

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:
    import fitz

MEDIA_EXTS = {".pdf", ".pptx", ".docx", ".png", ".jpg", ".jpeg"}
MIN_W, MIN_H = 200, 150  # below this it's a logo / bullet icon / rule line


def safe_id(path: Path) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", path.stem).strip("_")[:60] or "deck"


def sha(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def dims(data: bytes) -> tuple[int, int] | None:
    """(w, h) if PIL can read it, else None (kept, but size-unfiltered)."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def caption(text: str) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line[:42]


def save(outdir: Path, name: str, data: bytes, seen: set, max_per_file: int,
         count: int) -> dict | None:
    """Dedupe by bytes, size-filter, write. Returns the index row or None."""
    if count >= max_per_file:
        return None
    h = sha(data)
    if h in seen:
        return None
    size = dims(data)
    if size and (size[0] < MIN_W or size[1] < MIN_H):
        return None
    (outdir / name).write_bytes(data)
    seen.add(h)
    return {"file": name, "w": size[0] if size else None, "h": size[1] if size else None}


# --------------------------------------------------------------------------- pdf

def pdf_images(path: Path, outdir: Path, stem: str, o, anchors: set, seen: set) -> list[dict]:
    """Whole-page render, but only for pages whose layout a text dump would lose."""
    doc = fitz.open(path)
    rows, skipped = [], 0
    for no, page in enumerate(doc, 1):
        text = page.get_text().strip()
        cov = sum(fitz.Rect(i["bbox"]).get_area() for i in page.get_image_info())
        cov = min(1.0, cov / page.rect.get_area()) if page.rect.get_area() else 0.0
        vectors = len(page.get_drawings())
        # Diagram page: few chars, big bitmaps. Formula page: fraction bars and roots are
        # vector paths, invisible in the text dump but visible in get_drawings. Plain-text
        # page: none of those -> skip, materials.py already has its text.
        layout = cov >= o.min_coverage or vectors >= o.min_vectors
        if not o.all and not layout and no not in anchors:
            skipped += 1
            continue
        pix = page.get_pixmap(matrix=fitz.Matrix(o.zoom, o.zoom))
        row = save(outdir, f"{stem}-p{no:02d}.png", pix.tobytes("png"), seen, o.max_per_file, len(rows))
        if row:
            row.update(page=no, caption=caption(text), cited=no in anchors)
            rows.append(row)
        else:
            skipped += 1
    doc.close()
    if skipped:
        print(f"   {stem}: 跳过 {skipped} 页（纯文字/标题/过渡页，或重复图）")
    return rows


# --------------------------------------------------------------------------- pptx / docx

def zip_images(path: Path, outdir: Path, stem: str, seen: set,
               picks: list[tuple[str, str, str]], max_per_file: int) -> list[dict]:
    """picks = [(label, zip_path, tag)] in document order; shared dedupe/size/cap."""
    rows, done = [], 0
    with zipfile.ZipFile(path) as zf:
        for label, inner, tag in picks:
            if done >= max_per_file:
                break
            try:
                data = zf.read(inner)
            except KeyError:
                continue
            row = save(outdir, f"{stem}-{tag}{Path(inner).suffix.lower() or '.png'}",
                       data, seen, max_per_file, done)
            if row:
                row.update(page=label, caption=Path(inner).name)
                rows.append(row)
                done += 1
    return rows


RASTER = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}  # emf/wmf in PPT 课件 browsers can't show


def pptx_picks(path: Path) -> list[tuple[str, str, str]]:
    """[(slide_label, media_path_in_zip, tag)] via each slide's .rels, slides in order."""
    picks = []
    with zipfile.ZipFile(path) as zf:
        nums = sorted(int(re.match(r"ppt/slides/slide(\d+)\.xml$", n).group(1))
                      for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n))
        for n in nums:
            rels = zf.read(f"ppt/slides/_rels/slide{n}.xml.rels").decode("utf-8", "ignore")
            for k, t in enumerate(re.findall(r'Target="([^"]*media/[^"]+)"', rels), 1):
                if Path(t).suffix.lower() in RASTER:
                    picks.append((f"slide{n}", t.replace("../", "ppt/"), f"s{n}-{k}"))
    return picks


def docx_picks(path: Path) -> list[tuple[str, str, str]]:
    """media in the order the document body references it."""
    with zipfile.ZipFile(path) as zf:
        rels = zf.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
        id2t = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
        body = zf.read("word/document.xml").decode("utf-8", "ignore")
    order, seen_ids = [], set()
    for rid in re.findall(r'r:embed="([^"]+)"', body):
        t = id2t.get(rid)
        # docx targets are "media/x.png", relative to word/ -- pptx ones "../media/x.png"
        if t and t not in seen_ids and "/media/" in f"/{t}" and Path(t).suffix.lower() in RASTER:
            seen_ids.add(t)
            order.append(("word/" + t[3:]) if t.startswith("../") else "word/" + t)
    return [(f"图{i + 1}", t, f"d{i + 1}") for i, t in enumerate(order)]


# --------------------------------------------------------------------------- index

def write_index(results: list[dict], outdir: Path) -> None:
    total = sum(len(r["images"]) for r in results)
    lines = ["# 课件图片索引", "",
             f"> {len(results)} 份课件，共 {total} 张图。给讲义配图从这里选：",
             "> 每章 1–2 张，只嵌图本身携带正文没有的信息的页（示意图/公式排版/图表）。",
             "> `笔记已引用` = 该页在课件笔记里有 `## 第N页` 锚点，通常是值得配图的页。", ""]
    for r in results:
        head = f"## {r['source']}  —  {len(r['images'])} 张"
        if r.get("note"):
            head += f"（{r['note']}）"
        lines += [head, ""]
        if r["images"]:
            lines += ["| 图 | 来源 | 尺寸 | 首行/说明 | 笔记已引用 |",
                      "|---|---|---|---|---|"]
            for im in r["images"]:
                lines.append(f"| {im['file']} | {im.get('page', '')} "
                             f"| {im.get('w') or '?'}×{im.get('h') or '?'} "
                             f"| {im.get('caption', '')} | {'✔' if im.get('cited') else ''} |")
            lines.append("")
    (outdir / "INDEX.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- main

def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="课件目录或单个文件")
    ap.add_argument("-o", "--outdir", default="images", help="图片输出目录")
    ap.add_argument("-n", "--notes-dir", help="课件笔记目录（materials.py 的输出），用于标记已被引用的页")
    ap.add_argument("--all", action="store_true", help="不做筛选，渲染/提取全部页面")
    ap.add_argument("--min-coverage", type=float, default=0.25, help="位图覆盖率阈值")
    ap.add_argument("--min-vectors", type=int, default=8, help="矢量路径数阈值（公式排版页）")
    ap.add_argument("--zoom", type=float, default=2.0, help="PDF 渲染倍率（默认 2 → 1920×1080）")
    ap.add_argument("--max-per-file", type=int, default=60)
    o = ap.parse_args()

    files: list[Path] = []
    for raw in o.inputs:
        p = Path(raw)
        for f in (p.rglob("*") if p.is_dir() else [p]):
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS and Path(o.outdir) not in f.parents:
                files.append(f)
    files = sorted(set(files), key=lambda p: p.name.lower())
    if not files:
        print("[info] 没找到课件（支持 pdf/pptx/docx/png/jpg）")
        return 0

    outdir = Path(o.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    results = []

    for f in files:
        stem, ext = safe_id(f), f.suffix.lower()
        anchors: set = set()
        if o.notes_dir:
            note = Path(o.notes_dir) / f"{Path(f).stem}.md"
            if note.exists():
                anchors = {int(n) for n in
                           re.findall(r"^## 第(\d+)页", note.read_text(encoding="utf-8"), re.M)}
        if ext == ".pdf":
            rows = pdf_images(f, outdir, stem, o, anchors, seen)
        elif ext == ".pptx":
            rows = zip_images(f, outdir, stem, seen, pptx_picks(f), o.max_per_file)
        elif ext == ".docx":
            rows = zip_images(f, outdir, stem, seen, docx_picks(f), o.max_per_file)
        else:  # a loose image file: collect as-is
            rows = []
            row = save(outdir, f"{stem}{ext}", f.read_bytes(), seen, o.max_per_file, 0)
            if row:
                row.update(page="", caption=Path(f).stem)
                rows.append(row)
        note = ""
        if not rows:
            note = ("无版面页（纯文字 PDF，文字已在课件笔记里）" if ext == ".pdf"
                    else "无可提取图片（纯文字课件）")
        results.append({"source": f.name, "images": rows, "note": note})
        print(f"[{f.name[:40]}] {len(rows)} 张" + (f"  {note}" if note else ""))

    write_index(results, outdir)
    total = sum(len(r["images"]) for r in results)
    zero = [r["source"] for r in results if not r["images"]]
    print(f"\n[done] {total} 张 -> {outdir}/  INDEX.md")
    if zero:
        print(f"[info] {len(zero)} 份课件没有可提取图片（合法结果，讲义相应章节不插图）：")
        for z in zero:
            print(f"   - {z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
