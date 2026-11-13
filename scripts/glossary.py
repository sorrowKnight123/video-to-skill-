#!/usr/bin/env python3
"""Mine a term-correction glossary out of transcripts, with no model and no API.

The failure mode this targets: ASR mangles technical terms into several different-looking
variants, so the correct form never appears literally while its near-twins each show up two
or three times -- 谐振子 becomes 斜阵子 / 邪真子 / 协真子, 傅里叶变换 becomes 复利液变换.

Two signals find them, because homophone substitution is what ASR actually does:

1. **Same pronunciation.** 斜阵子 and 谐振子 differ in two characters but are pronounced
   identically (xie zhen zi); 复利液变换 and 傅里叶变换 differ in three, again identical.
   Edit distance is the wrong metric for Chinese -- pinyin is the right one.
2. **One character apart.** Catches non-homophone slips like 厄米算数 -> 厄米算符. Only for
   4+ characters: on a short window one differing character is too much of the string.

Candidates then need a winner, from either:
- **Cluster**: inside one transcript a form that appears 20 times is almost always the right
  one, and its rare twins are mishearings of it.
- **Reference**: a homophone of a term that also appears in the courseware is almost
  certainly that term, even if the correct form never appears in the audio at all.

Three guards keep the noise down, all needed in practice:
- the "wrong" form must not itself appear in the courseware (分子 "molecule" is a real word,
  not a mishearing of 粒子);
- both sides must be *maximal* n-grams -- extending them by one character must not make them
  far more frequent. Without this every window of a term gets proposed (本征函, 征方程);
- the differing character must not be a glue character (了, 的, 是 ...).

Pure stdlib except `pypinyin` (pure Python, ~2MB). Without it the pinyin signal is skipped
and only the one-character signal runs, which finds far less.

    python glossary.py transcripts/ -o 术语表.auto.json --reference 课件笔记/
    python glossary.py transcripts/ -o 术语表.auto.json        # no courseware: cluster only
    python glossary.py --selftest

The output keeps the format `transcribe.py --apply-glossary` already reads, so the workflow
is: generate -> read the evidence report -> delete what you disagree with -> apply.
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".srt", ".markdown"}
KEEP = re.compile(r"[\u4e00-\u9fff0-9a-zA-Z]")
SENT_SPLIT = re.compile(r"[。！？；：\n\r,，.、!?;:]+")
FUNCTION_CHARS = set("的了一是在有和就不人都上也很到说要去你会着没看好这那们为我他来她它把被给让从对与及或但而且所以因为如果虽然可以可能应该需要这样怎样么什时候然后因此但并还再又更最")


def read_text(paths: list[str]) -> str:
    chunks = []
    for raw in paths:
        p = Path(raw)
        files = sorted(f for f in p.rglob("*") if f.suffix.lower() in TEXT_EXTS) if p.is_dir() else [p]
        for f in files:
            if f.is_file():
                chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def pinyin_of(text: str) -> list[str] | None:
    """Per-character pinyin, aligned 1:1 with `text`; None when pypinyin is unavailable."""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None
    # errors="default" keeps non-Chinese characters as themselves, which keeps the result
    # aligned with `text`. errors="ignore" silently drops them and desyncs every offset.
    py = lazy_pinyin(text, style=Style.NORMAL, errors="default")
    if len(py) != len(text):
        py = []
        for c in text:
            one = lazy_pinyin(c, style=Style.NORMAL, errors="default")
            py.append(one[0] if one else c)
    return py if len(py) == len(text) else None


class Corpus:
    """n-gram statistics, plus indexes built over *maximal* n-grams only."""

    def __init__(self, text: str, min_len: int, max_len: int,
                 maximal_only: bool = False, fragment_ratio: float = 3.0):
        self.count: collections.Counter = collections.Counter()
        self.contexts: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
        self.by_pinyin: dict[tuple[str, ...], set[str]] = collections.defaultdict(set)
        self.by_mask: dict[str, set[str]] = collections.defaultdict(set)
        self.pinyin: dict[str, tuple[str, ...]] = {}
        self.has_pinyin = pinyin_of("测试") is not None

        sentences = []
        for sentence in SENT_SPLIT.split(text):
            clean = "".join(KEEP.findall(sentence))
            if len(clean) >= min_len:
                sentences.append(clean)

        # pass 1: counts and contexts for everything
        for clean in sentences:
            for n in range(min_len, min(max_len, len(clean)) + 1):
                for i in range(len(clean) - n + 1):
                    gram = clean[i:i + n]
                    self.count[gram] += 1
                    self.contexts[gram].add(
                        (clean[i - 1] if i else "", clean[i + n] if i + n < len(clean) else "")
                    )

        # pass 2: indexes over the grams worth proposing, using pass-1 counts
        for clean in sentences:
            py = pinyin_of(clean) if self.has_pinyin else None
            for n in range(min_len, min(max_len, len(clean)) + 1):
                for i in range(len(clean) - n + 1):
                    gram = clean[i:i + n]
                    if maximal_only and self.is_left_fragment(gram):
                        continue
                    for pos in range(n):
                        self.by_mask[gram[:pos] + "\x00" + gram[pos + 1:]].add(gram)
                    if py:
                        key = tuple(py[i:i + n])
                        self.by_pinyin[key].add(gram)
                        self.pinyin.setdefault(gram, key)

    def is_left_fragment(self, gram: str, ratio: float = 1.0) -> bool:
        """True when the previous character extends it just as often, i.e. the gram is the
        tail of a longer word (量本征值 always follows 学 -> it is part of 力学量本征值).
        Only the left side is checked: a term can legitimately be followed by 的 every
        time it appears, but it can never always be preceded by the same stray character."""
        freq = self.count.get(gram, 0)
        if not freq:
            return True
        for prev, _ in self.contexts.get(gram, ()):
            if prev and self.count.get(prev + gram, 0) >= freq * ratio:
                return True
        return False

    def is_fragment(self, gram: str, ratio: float) -> bool:
        """True when a 1-char extension is this much more frequent, i.e. the gram is a
        window across two words (今天学 -> 天学) rather than a term of its own."""
        freq = self.count[gram]
        if not freq:
            return True
        for prev, nxt in self.contexts.get(gram, ()):
            if prev and self.count.get(prev + gram, 0) >= freq * ratio:
                return True
            if nxt and self.count.get(gram + nxt, 0) >= freq * ratio:
                return True
        return False


def one_char_apart(a: str, b: str) -> bool:
    return len(a) == len(b) and a != b and sum(x != y for x, y in zip(a, b)) == 1


def differs_only_in_function_char(a: str, b: str) -> bool:
    for x, y in zip(a, b):
        if x != y and (x in FUNCTION_CHARS or y in FUNCTION_CHARS):
            return True
    return False


def mine(transcript: str, reference: str, args) -> tuple[list[dict], bool]:
    t = Corpus(transcript, args.min_len, args.max_len, maximal_only=False)
    r = (Corpus(reference, args.min_len, args.max_len, maximal_only=True,
                fragment_ratio=args.fragment_ratio) if reference else None)
    # Transcript-side winners (cluster mode) must be maximal too, else 征方程 beats 本征方程.
    t_max = set(g for g in t.count if not t.is_fragment(g, args.fragment_ratio))

    proposals: dict[str, dict] = {}

    def offer(wrong: str, right: str, source: str, signal: str, force: bool = False) -> None:
        if wrong == right or differs_only_in_function_char(wrong, right):
            return
        # Terms do not contain glue characters. Rejecting them removes a whole noise class
        # (输的->数的, 熟悉的->熟悉的, ...的本征 windows) at no cost to real fixes.
        if any(c in FUNCTION_CHARS for c in wrong + right):
            return
        # Attested in the courseware => a real word, not a mishearing. signal "misheard"
        # (force=True) is the exception: the ASR got it wrong every single time, so the
        # courseware contradicting the transcript is exactly the evidence for the fix.
        if not force and r is not None and wrong in r.count:
            return
        if t.is_fragment(wrong, args.fragment_ratio):
            return
        w = t.count[wrong]
        r_count = (r.count[right] if source == "reference" else t.count[right])
        if source == "reference":
            if w > args.max_variant_freq or r_count < args.ref_min_freq:
                return
            if r.is_left_fragment(right):
                return
        else:
            if w > args.max_variant_freq or r_count < args.min_correct_freq:
                return
            if r_count < w * args.min_ratio or right not in t_max:
                return
        score = w * (2 if source == "reference" else 1)
        old = proposals.get(wrong)
        if old is None or score > old["score"]:
            proposals[wrong] = {
                "wrong": wrong, "right": right, "source": source, "signal": signal,
                "wrong_count": w, "right_count": r_count, "score": score,
            }

    for gram, freq in t.count.items():
        if freq > args.max_variant_freq or len(gram) < args.min_len:
            continue
        key = t.pinyin.get(gram)
        if key is not None:  # signal 1: same pronunciation
            if r is not None:
                for cand in r.by_pinyin.get(key, ()):
                    if cand != gram:
                        offer(gram, cand, "reference", "homophone")
            for cand in t.by_pinyin.get(key, ()):
                if cand != gram and cand in t_max:
                    offer(gram, cand, "cluster", "homophone")
        if r is not None and gram not in r.count and key is not None:
            # signal 3: consistent mishearing. The courseware spells this term one way and
            # never the way the transcript does, no matter how often the transcript does.
            for cand in r.by_pinyin.get(key, ()):
                if cand != gram and r.count[cand] >= args.min_correct_freq:
                    offer(gram, cand, "reference", "misheard", force=True)
        if len(gram) < 4:  # signal 2: one character apart, only when that is a small share
            continue
        for pos in range(len(gram)):
            mask = gram[:pos] + "\x00" + gram[pos + 1:]
            if r is not None:
                for cand in r.by_mask.get(mask, ()):
                    if one_char_apart(gram, cand):
                        offer(gram, cand, "reference", "1-char")
            for cand in t.by_mask.get(mask, ()):
                if one_char_apart(gram, cand) and cand in t_max:
                    offer(gram, cand, "cluster", "1-char")

    ranked = sorted(proposals.values(), key=lambda p: (-p["score"], -len(p["wrong"])))
    kept: list[dict] = []
    for p in ranked:
        fragment = any(
            len(q["wrong"]) > len(p["wrong"]) and p["wrong"] in q["wrong"]
            and p["right"] in q["right"] and q["wrong_count"] == p["wrong_count"]
            for q in ranked
        )
        if not fragment:
            kept.append(p)
    kept.sort(key=lambda p: (-p["score"], -p["right_count"]))
    return kept[: args.top], t.has_pinyin


def adjudicate(rows: list[dict], args) -> list[dict]:
    """Let a chat model judge each candidate pair.

    The miner is deterministic and knows frequency and pronunciation; it does not know that
    常温 is a real word or that 搅动量 must be 角动量 in a mechanics lecture. A small model
    call per batch closes that gap. Rejected pairs are dropped, "fix" verdicts replace the
    right-hand side, and every kept pair keeps its evidence for the report.
    """
    import os
    import urllib.request

    key = os.environ.get("GLOSSARY_API_KEY", "")
    base = os.environ.get("GLOSSARY_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("GLOSSARY_MODEL", "gpt-4o-mini")
    if not key:
        print("[warn] --llm needs GLOSSARY_API_KEY; skipping adjudication", file=sys.stderr)
        return rows

    top, rest = rows[: args.llm_top], rows[args.llm_top:]
    kept: list[dict] = []
    domain = args.domain or "一门大学理工科网课"
    for start in range(0, len(top), 20):
        batch = top[start:start + 20]
        listing = "\n".join(
            f"{i + 1}. {x['wrong']} -> {x['right']}  (转录出现{x['wrong_count']}次)" for i, x in enumerate(batch)
        )
        prompt = (
            f"以下是语音转写纠错候选（ASR 把术语听成了错别字）。课程主题：{domain}。\n"
            f"逐条判断：错误的那个词是不是正确词的同音/近音误听？\n"
            f"- 保留：确实是误听（输出 verdict=keep）\n"
            f"- 否决：错法或正确写法本身是正常词/搭配不通/毫无关系（verdict=reject）\n"
            f"- 修正：方向对但正确写法不准（verdict=fix，并给出 corrected）\n"
            f"只输出 JSON 数组，不要解释：[{{\"i\":1,\"verdict\":\"keep\"}},...]\n\n{listing}"
        )
        body = json.dumps({"model": model, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
            content = reply["choices"][0]["message"]["content"]
            verdicts = json.loads(re.search(r"\[.*\]", content, re.S).group(0))
        except Exception as e:
            print(f"[warn] adjudication failed ({e}); keeping this batch unreviewed", file=sys.stderr)
            kept.extend(batch)
            continue
        for v in verdicts:
            idx = int(v.get("i", 0)) - 1
            if not 0 <= idx < len(batch):
                continue
            x, verdict = batch[idx], str(v.get("verdict", "keep")).lower()
            if verdict == "reject":
                continue
            if verdict == "fix" and v.get("corrected"):
                x = dict(x, right=str(v["corrected"]), source="reference", signal="llm-fix")
            kept.append(x)
    print(f"[llm] adjudicated {len(top)}: kept {len(kept)}, rejected {len(top) - len(kept)}; "
          f"{len(rest)} unreviewed candidates dropped (--llm-top to raise)")
    return kept


def selftest() -> int:
    transcript = (
        "谐振子非常重要。谐振子有三个特点。我们来看谐振子。谐振子的基态是零点能。"
        "这里一个斜阵子出来了。再说邪真子。协真子也有。"
        "转置运算。转置就是交换位置。刚才说到转质。"
        "傅里叶变换很关键。傅里叶变换把坐标变到动量。这里读成了复利液变换。"
        "厄米算符的本征值是实数。厄米算符这一点要记住。刚才写的厄米算数是错的。"
        "今天学习量子力学。明天学习统计物理。"
    )
    reference = ("一维线性谐振子的能级。谐振子的波函数。矩阵的转置。转置与复共轭。厄米算符的本征值。厄米算符的本征函数。傅里叶变换的定义。傅里叶变换的逆变换。今天学习。")
    args = argparse.Namespace(min_len=2, max_len=6, min_correct_freq=3, max_variant_freq=2,
                              min_ratio=2.0, ref_min_freq=2, top=100, fragment_ratio=3.0)
    rows, has_py = mine(transcript, reference, args)
    got = {p["wrong"]: p["right"] for p in rows}
    expected = {"斜阵子": "谐振子", "邪真子": "谐振子", "协真子": "谐振子",
                "转质": "转置", "复利液变换": "傅里叶变换", "厄米算数": "厄米算符"}
    bad = {k: v for k, v in expected.items() if got.get(k) != v}
    noise = [k for k in got if k in {"天学", "天学习", "学习"}]
    print(f"selftest: pinyin={'on' if has_py else 'off'}, "
          f"{len(expected) - len(bad)}/{len(expected)} pairs recovered, {len(noise)} fragment(s) leaked")
    for k, v in bad.items():
        print(f"  MISSED {k} -> {v} (got {got.get(k)})")
    if bad or noise:
        print(f"  noise: {noise}")
        return 1
    print("ok")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="transcript files or directories")
    ap.add_argument("-o", "--out", default="术语表.auto.json", help="glossary to write")
    ap.add_argument("--reference", action="append", default=[],
                    help="courseware notes / textbook excerpt; can repeat")
    ap.add_argument("--report", help="evidence report path (default: <out>.md)")
    ap.add_argument("--min-len", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=6)
    ap.add_argument("--min-correct-freq", type=int, default=3, help="a cluster winner must appear this often")
    ap.add_argument("--max-variant-freq", type=int, default=2, help="a typo must appear at most this often")
    ap.add_argument("--min-ratio", type=float, default=2.0, help="winner/variant frequency ratio (cluster mode)")
    ap.add_argument("--ref-min-freq", type=int, default=2, help="how often a courseware term must appear")
    ap.add_argument("--fragment-ratio", type=float, default=3.0,
                    help="skip a candidate whose 1-char extension is this much more frequent")
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--llm", action="store_true",
                    help="adjudicate the top candidates with a chat model (OpenAI-compatible; "
                         "env: GLOSSARY_API_KEY, GLOSSARY_BASE_URL, GLOSSARY_MODEL)")
    ap.add_argument("--llm-top", type=int, default=60, help="how many top candidates to adjudicate")
    ap.add_argument("--domain", default="", help="one-line course description for the LLM")
    ap.add_argument("--dry-run", action="store_true", help="print the table, write nothing")
    args = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    transcript = read_text(args.inputs)
    reference = read_text(args.reference) if args.reference else ""
    if len(transcript) < 500:
        raise SystemExit("[error] transcript too short to mine (need ~500+ chars)")

    rows, has_pinyin = mine(transcript, reference, args)
    if not has_pinyin:
        print("[warn] pypinyin not installed -- the homophone signal is off, so only "
              "one-character slips are found.  pip install pypinyin", file=sys.stderr)
    if not rows:
        print("[info] no candidates. Either the transcript is clean, or lower "
              "--min-correct-freq / --max-variant-freq.")
        return 0

    ref_n = sum(1 for x in rows if x["source"] == "reference")
    print(f"[found] {len(rows)} candidate(s): {ref_n} anchored on courseware, "
          f"{len(rows) - ref_n} from frequency clusters")
    for x in rows[:15]:
        print(f"  {x['wrong']:>12} -> {x['right']:<12} [{x['source']:<9}/{x['signal']:<9}] "
              f"{x['wrong_count']}x vs {x['right_count']}x")
    if len(rows) > 15:
        print(f"  ... and {len(rows) - 15} more")

    if args.llm and rows:
        rows = adjudicate(rows, args)

    if args.dry_run:
        print("[dry-run] nothing written")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    glossary = {"_readme": "自动生成：键=转录里的错法，值=正确写法。先看同名 .md 证据表，"
                           "删掉不同意的条目再 --apply-glossary。"}
    glossary.update({x["wrong"]: x["right"] for x in rows})
    out.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = Path(args.report) if args.report else out.with_suffix(".md")
    basis = {"reference": "课件锚定", "cluster": "高频同形"}
    sig = {"homophone": "同音", "1-char": "差一字", "misheard": "一致误听", "llm-fix": "LLM修正"}
    lines = ["# 自动生成的术语纠错候选", "",
             f"来源：{len(transcript)} 字转录" + (f" + {len(reference)} 字课件" if reference else "（无课件）"),
             "", "| 错法 | 正确 | 依据 | 信号 | 错法出现 | 正确出现 |", "|---|---|---|---|---|---|"]
    for x in rows:
        lines.append(f"| {x['wrong']} | {x['right']} | {basis[x['source']]} | {sig[x['signal']]} | "
                     f"{x['wrong_count']} | {x['right_count']} |")
    lines += ["", "**怎么用**：删掉你不同意的行对应的 JSON 条目，然后", "",
              "```bash", f"python scripts/transcribe.py --apply-glossary {out.name} transcripts/ --dry-run",
              f"python scripts/transcribe.py --apply-glossary {out.name} transcripts/", "```"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[out] {out}  +  {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
