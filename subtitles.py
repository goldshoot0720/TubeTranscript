#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕處理：切句、翻譯、匯出 SRT / VTT / ASS / TXT。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "translate.json"
SEP = " ||| "
SENT_END = ".!?。！？…．"
CLAUSE_END = ",;:、，；：—–"
NO_SPACE_LANGS = ("ja", "zh", "th", "yue")

_cache_lock = threading.Lock()
_cache: dict[str, str] | None = None


# ---------------------------------------------------------------- 翻譯快取


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(CACHE_PATH.read_text("utf-8"))
        if not isinstance(_cache, dict):
            _cache = {}
    except Exception:
        _cache = {}
    return _cache


def _flush_cache() -> None:
    if _cache is None:
        return
    try:
        if not CACHE_PATH.parent.exists():
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False), "utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _key(text: str, sl: str, tl: str) -> str:
    return hashlib.sha1(f"{sl}|{tl}|{text}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 翻譯


class TranslateError(RuntimeError):
    pass


def _google_once(text: str, sl: str, tl: str) -> str:
    """用 urllib 呼叫 Google 翻譯的免費端點（requests 的 header 特徵會被回 429）。"""
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={sl or 'auto'}&tl={tl}&dt=t&q={urllib.parse.quote(text)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise TranslateError("rate-limited")
        raise TranslateError(f"HTTP {exc.code}")
    data = json.loads(raw)
    if not data or not data[0]:
        raise TranslateError("empty response")
    out = []
    for seg in data[0]:
        if seg and seg[0]:
            out.append(seg[0])
    return "".join(out).strip()


def _google(text: str, sl: str, tl: str, tries: int = 4) -> str:
    last = None
    for attempt in range(tries):
        try:
            return _google_once(text, sl, tl)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 * (2**attempt))
    raise TranslateError(f"翻譯失敗：{last}")


def _chunks(indices: Sequence[int], texts: Sequence[str], max_items: int = 8, max_chars: int = 900) -> list[list[int]]:
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i in indices:
        n = len(texts[i]) + len(SEP)
        if cur and (len(cur) >= max_items or size + n > max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        out.append(cur)
    return out


def translate_texts(
    texts: Sequence[str],
    sl: str,
    tl: str,
    on_progress: Callable[[int, int], None] | None = None,
    workers: int = 3,
) -> tuple[list[str], list[str]]:
    """回傳 (譯文列表, 警告列表)。單句失敗時譯文為空字串。"""
    cache = _load_cache()
    results: list[str] = [""] * len(texts)
    warnings: list[str] = []
    pending: list[int] = []

    for i, t in enumerate(texts):
        k = _key(t, sl, tl)
        if k in cache:
            results[i] = cache[k]
        elif not t.strip():
            results[i] = ""
        else:
            pending.append(i)

    if not pending:
        if on_progress:
            on_progress(len(texts), len(texts))
        return results, warnings

    done = len(texts) - len(pending)
    lock = threading.Lock()

    def bump(n: int = 1) -> None:
        nonlocal done
        with lock:
            done += n
            if on_progress:
                on_progress(done, len(texts))

    def work(indices: list[int]) -> None:
        items = [texts[i].replace(SEP.strip(), " ").strip() for i in indices]
        joined = SEP.join(items)
        try:
            out = _google(joined, sl, tl)
            parts = [p.strip() for p in re.split(r"\s*[|｜]{2,}\s*", out)]
            parts = [p for p in parts if p != ""]
            if len(parts) == len(items):
                for idx, part in zip(indices, parts):
                    results[idx] = part
                bump(len(indices))
                return
            raise TranslateError("batch split mismatch")
        except Exception:
            pass
        # 退回逐句翻譯
        for idx, item in zip(indices, items):
            try:
                results[idx] = _google(item, sl, tl)
            except Exception:
                results[idx] = ""
            bump(1)
            time.sleep(0.05)

    groups = _chunks(pending, texts)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, g): g for g in groups}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                for idx in futures[fut]:
                    if not results[idx]:
                        bump(1)

    with _cache_lock:
        for i, t in enumerate(texts):
            if results[i]:
                cache[_key(t, sl, tl)] = results[i]
    _flush_cache()

    missing = sum(1 for i in pending if not results[i])
    if missing:
        warnings.append(f"{missing} 句翻譯失敗，已保留原文")
    return results, warnings


# ---------------------------------------------------------------- 切句


def join_words(words: Sequence[dict], lang: str) -> str:
    if lang in NO_SPACE_LANGS:
        return "".join(w["t"] for w in words)
    return " ".join(w["t"] for w in words)


_LIMITS = {
    "ko": (40, 10, 6000),   # 韓文音節較寬，一行約 20 字
    "ja": (42, 8, 6000),
    "zh": (42, 8, 6000),
    "th": (56, 10, 6000),
}


def _limits(lang: str) -> tuple[int, int, int]:
    """(單句最大字數, 最小字數, 最大毫秒)"""
    return _LIMITS.get(lang, (84, 20, 6500))


def _clean(text: str, lang: str) -> str:
    if lang in NO_SPACE_LANGS:
        return text.strip()
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.!?;:%\)\]}」』])", r"\1", text)


def _make(words: Sequence[dict], lang: str, sent: int = -1, sent_text: str = "") -> dict:
    return {
        "start": words[0]["s"],
        "end": max(words[-1]["e"], words[0]["s"] + 300),
        "orig": _clean(join_words(words, lang), lang),
        "words": [dict(w) for w in words],
        "sent": sent,
        "sent_text": sent_text,
    }


def _split_sentences(words: Sequence[dict], max_gap: int) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > max_gap:
            out.append(cur)
            cur = []
        cur.append(w)
        if w["t"] and w["t"][-1] in SENT_END:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [s for s in out if s]


def _split_clauses(words: Sequence[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        cur.append(w)
        if w["t"] and w["t"][-1] in CLAUSE_END:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _pack(words: Sequence[dict], lang: str, max_chars: int, max_dur: int) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for w in words:
        wl = len(w["t"]) + (0 if lang in NO_SPACE_LANGS else 1)
        if cur and (size + wl > max_chars or w["e"] - cur[0]["s"] > max_dur):
            out.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += wl
    if cur:
        out.append(cur)
    return out


def _pack_clauses(
    clauses: Sequence[Sequence[dict]], lang: str, max_chars: int, max_dur: int
) -> list[list[dict]]:
    """以子句為單位貪心合併，盡量在逗號處斷行；單一子句過長才硬切。"""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0

    def flush() -> None:
        nonlocal cur, size
        if cur:
            out.append(cur)
        cur, size = [], 0

    for clause in clauses:
        text = join_words(clause, lang)
        csize = len(text) + (0 if lang in NO_SPACE_LANGS else 1)
        if csize > max_chars:
            flush()
            out.extend(_pack(clause, lang, max_chars, max_dur))
            continue
        dur = (clause[-1]["e"] - (cur[0]["s"] if cur else clause[0]["s"]))
        if cur and (size + csize > max_chars or dur > max_dur):
            flush()
        cur.extend(clause)
        size += csize
    flush()
    return out


def segment(
    words: Sequence[dict],
    lang: str,
    max_dur: int | None = None,
    max_gap: int = 1200,
) -> list[dict]:
    """先依句末標點切句；過長的句子再依逗號等停頓切成子句，最後才硬切。"""
    max_chars, min_chars, default_dur = _limits(lang)
    max_dur = max_dur or default_dur

    cues: list[dict] = []
    for si, sent in enumerate(_split_sentences(words, max_gap)):
        text = _clean(join_words(sent, lang), lang)
        if len(text) <= max_chars and sent[-1]["e"] - sent[0]["s"] <= max_dur:
            cues.append(_make(sent, lang, si, text))
            continue
        for piece in _pack_clauses(_split_clauses(sent), lang, max_chars, max_dur):
            cues.append(_make(piece, lang, si, text))

    # 合併過短句
    merged: list[dict] = []
    for c in cues:
        if merged:
            prev = merged[-1]
            too_short = (c["end"] - c["start"]) < 650
            close = c["start"] - prev["end"] < 400
            short_text = len(prev["orig"]) < min_chars
            if (too_short or short_text) and close and len(prev["orig"]) + len(c["orig"]) <= max_chars * 1.4:
                prev["end"] = c["end"]
                prev["orig"] = _clean(join_words(prev["words"] + c["words"], lang), lang)
                prev["words"].extend(c["words"])
                if prev["sent"] != c["sent"]:
                    prev["sent"] = -1
                    prev["sent_text"] = ""
                continue
        merged.append(c)

    # 去除重疊、保證時間單調
    for i, c in enumerate(merged):
        if i + 1 < len(merged):
            nxt = merged[i + 1]
            if c["end"] > nxt["start"] - 40:
                c["end"] = max(c["start"] + 400, nxt["start"] - 40)
        if c["end"] <= c["start"]:
            c["end"] = c["start"] + 800

    return merged


# ---------------------------------------------------------------- 翻譯對齊

BREAK_CHARS = "，。、；：！？…,.;:!?"


def _snap(text: str, target: int, lo: int, hi: int) -> int:
    """在 [lo, hi] 內找最接近 target 的標點邊界；找不到就照原位置切。"""
    n = len(text)
    lo = max(1, min(lo, n))
    hi = max(lo, min(hi, n))
    best, best_d = None, 10**9
    for p in range(lo, hi + 1):
        if text[p - 1] in BREAK_CHARS:
            d = abs(p - target)
            if d <= best_d:
                best, best_d = p, d
    if best is not None and best_d <= max(4, n // 4):
        return best
    return max(lo, min(hi, target))


def slice_translations(full: str, ratios: Sequence[tuple[float, float]]) -> list[str]:
    """依時間比例把整句譯文切成多段，盡量切在標點上。"""
    n = len(full)
    k = len(ratios)
    if k <= 1 or n <= 2:
        return [full.strip() for _ in ratios]
    if n < 3 * k:
        return [full.strip()] + [""] * (k - 1)

    cuts = [0]
    for idx in range(k - 1):
        target = int(round(n * ratios[idx][1]))
        lo = cuts[-1] + 1
        hi = n - (k - 1 - idx)
        cuts.append(_snap(full, target, lo, hi))
    cuts.append(n)

    out = []
    for i in range(k):
        piece = full[cuts[i]:cuts[i + 1]].strip(" ，、；：,;:")
        out.append(piece or full[cuts[i]:cuts[i + 1]])
    return out


def apply_translations(cues: Sequence[dict], texts: Sequence[str], translations: Sequence[str]) -> None:
    """同一個來源句子的多個字幕，共用整句譯文，再依時間比例分配。"""
    tmap = {t: tr for t, tr in zip(texts, translations)}
    groups: dict[int, list[dict]] = {}
    for idx, c in enumerate(cues):
        sid = int(c.get("sent", -1))
        key = sid if sid >= 0 else -1000000 - idx
        groups.setdefault(key, []).append(c)

    for sid, group in groups.items():
        full = tmap.get(group[0].get("sent_text") or group[0]["orig"], "") or ""
        if not full:
            for c in group:
                c["trans"] = ""
            continue
        if sid < 0 or len(group) == 1:
            for c in group:
                c["trans"] = full
            continue
        s0, s1 = group[0]["start"], group[-1]["end"]
        span = max(1, s1 - s0)
        ratios = [((c["start"] - s0) / span, (c["end"] - s0) / span) for c in group]
        for c, piece in zip(group, slice_translations(full, ratios)):
            c["trans"] = piece


def cues_from_text(text: str, duration_ms: int, lang: str) -> list[dict]:
    """沒有詞級時間戳時的備援：依句子長度均分時間。"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s*", text or "") if p.strip()]
    if not parts:
        return []
    total = sum(len(p) for p in parts) or 1
    cues = []
    t = 0
    for p in parts:
        span = int(duration_ms * len(p) / total)
        cues.append({"start": t, "end": t + max(span, 600), "orig": p, "words": []})
        t += span
    return cues


# ---------------------------------------------------------------- 格式化


def _ts(ms: int, fmt: str = "srt") -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    if fmt == "srt":
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    if fmt == "vtt":
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    # ass
    cs = ms // 10
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _pick(cue: dict, mode: str) -> list[str]:
    orig = (cue.get("orig") or "").strip()
    trans = (cue.get("trans") or "").strip()
    if mode == "orig":
        return [orig] if orig else []
    if mode == "trans":
        return [trans or orig] if (trans or orig) else []
    return [x for x in (orig, trans) if x]


def to_srt(cues: Iterable[dict], mode: str = "both") -> str:
    out = []
    for i, c in enumerate(cues, 1):
        lines = _pick(c, mode)
        if not lines:
            continue
        out.append(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def to_vtt(cues: Iterable[dict], mode: str = "both") -> str:
    out = ["WEBVTT", ""]
    for c in cues:
        lines = _pick(c, mode)
        if not lines:
            continue
        out.append(f"{_ts(c['start'], 'vtt')} --> {_ts(c['end'], 'vtt')}")
        out.append("\n".join(lines))
        out.append("")
    return "\n".join(out)


ASS_HEADER = """[Script Info]
Title: TubeTranscript 雙語字幕
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dual,PingFang TC,58,&H004DD8FF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3.4,1.8,2,80,80,72,1
Style: Orig,PingFang TC,40,&H00C9EFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,1.4,2,80,80,72,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ")


def to_ass(cues: Iterable[dict], mode: str = "both") -> str:
    rows = []
    for c in cues:
        orig = _ass_escape((c.get("orig") or "").strip())
        trans = _ass_escape((c.get("trans") or "").strip())
        if mode == "orig" or not trans:
            body = f"{{\\rOrig}}{orig}"
        elif mode == "trans":
            body = f"{{\\rDual}}{trans}"
        else:
            body = f"{{\\rOrig}}{orig}\\N{{\\rDual}}{trans}"
        if not body:
            continue
        rows.append(
            f"Dialogue: 0,{_ts(c['start'], 'ass')},{_ts(c['end'], 'ass')},Dual,,0,0,0,,{body}"
        )
    return ASS_HEADER + "\n".join(rows) + "\n"


def to_txt(cues: Iterable[dict], mode: str = "both") -> str:
    rows = []
    for c in cues:
        lines = _pick(c, mode)
        if not lines:
            continue
        stamp = _ts(c["start"]).replace(",", ".")
        rows.append(f"[{stamp}] " + " / ".join(lines))
    return "\n".join(rows) + "\n"


EXPORTERS = {
    "srt": (to_srt, "srt", "application/x-subrip"),
    "vtt": (to_vtt, "vtt", "text/vtt"),
    "ass": (to_ass, "ass", "text/plain; charset=utf-8"),
    "txt": (to_txt, "txt", "text/plain; charset=utf-8"),
}
