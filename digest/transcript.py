import re
import time

import requests

from .util import log, similarity, split_sentences, strip_html, tokens

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
H = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SPAN_RE = re.compile(r'(?s)<span id="sentenceid_\d+"[^>]*class="[^"]*transcript-text[^"]*"[^>]*>(.*?)</span>')
TITLE_RE = re.compile(r"(?is)<title>(.*?)</title>")


class TranscriptError(RuntimeError):
    pass


def _get(url: str, timeout: int, tries: int = 3) -> str:
    last = "unknown"
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=timeout)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)[:120]
        time.sleep(2 + i * 4)
    raise TranscriptError(f"fetch failed: {last}")


def build_index(base: str, timeout: int, max_pages: int = 30) -> dict[str, set[str]]:
    """抓取转录站目录页，返回 slug -> token 集合。"""
    index: dict[str, set[str]] = {}
    pages = 0
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}?page={page}"
        try:
            html = _get(url, timeout, tries=2)
        except TranscriptError:
            break
        slugs = set(re.findall(r'href="(/podcasts/[^"#?]+/[^"#?]+)"', html))
        new = 0
        for s in slugs:
            if s not in index:
                index[s] = tokens(s.split("/")[-1])
                new += 1
        pages += 1
        if new == 0 and page > 1:
            break
        time.sleep(0.4)
    log("transcript index", f"pages={pages}", f"entries={len(index)}")
    return index


def match_slug(index: dict[str, set[str]], title: str, used: set[str], threshold: float = 0.6) -> tuple[str, float]:
    core = re.split(r"\s*\|\s*", title)[0]
    tk = tokens(core)
    best, best_score = "", 0.0
    for slug, st in index.items():
        if slug in used:
            continue
        score = similarity(tk, st)
        if score > best_score:
            best, best_score = slug, score
    if best_score < threshold:
        return "", best_score
    return best, best_score


def fetch_transcript(site_root: str, slug: str, expected_title: str, timeout: int) -> str:
    url = site_root.rstrip("/") + slug
    html = _get(url, timeout)
    m = TITLE_RE.search(html)
    page_title = strip_html(m.group(1)) if m else ""
    if page_title and similarity(tokens(expected_title), tokens(page_title)) < 0.5:
        raise TranscriptError(f"title mismatch: {page_title[:60]}")
    raw_spans = SPAN_RE.findall(html)
    if not raw_spans:
        raw_spans = re.findall(r'(?s)<span[^>]*class="[^"]*transcript-text[^"]*"[^>]*>(.*?)</span>', html)
    text_parts = [strip_html(s) for s in raw_spans]
    text = " ".join(p for p in text_parts if p)
    words = len(text.split())
    if words < 400:
        raise TranscriptError(f"transcript too short: words={words}")
    sentences = split_sentences(text)
    if len(sentences) < 20:
        raise TranscriptError(f"too few sentences: {len(sentences)}")
    return text
