import re
import time
import base64
import os
from urllib.parse import quote

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

# 备用源：开源转录仓库（YouTube 字幕整理版），按文件名匹配后从 raw 拉取
FALLBACK_REPO = "mrlong0129/huberman-lab-transcripts"
FALLBACK_BRANCH = "main"
FALLBACK_TXT_DIR = "transcripts/"
RAW_BASE = f"https://raw.githubusercontent.com/{FALLBACK_REPO}/{FALLBACK_BRANCH}/"


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


def build_fallback_index(token: str, timeout: int = 90) -> dict[str, dict]:
    """列出备用仓库的 transcripts/*.txt，返回 路径 -> {tokens, sha}。"""
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{FALLBACK_REPO}/git/trees/{FALLBACK_BRANCH}?recursive=1"
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        log("fallback index failed", f"http={r.status_code}")
        return {}
    index: dict[str, dict] = {}
    for node in r.json().get("tree", []):
        path = node.get("path", "")
        if node.get("type") == "blob" and path.startswith(FALLBACK_TXT_DIR) and path.endswith(".txt"):
            stem = path[len(FALLBACK_TXT_DIR) : -4]
            index[path] = {"tokens": tokens(stem), "sha": node.get("sha", "")}
    log("fallback index", f"entries={len(index)}")
    return index


def match_fallback(index: dict[str, dict], title: str, used: set[str], threshold: float = 0.75) -> tuple[str, float]:
    core = re.split(r"\s*\|\s*", title)[0]
    tk = tokens(core)
    best, best_score = "", 0.0
    for path, meta in index.items():
        if path in used:
            continue
        score = similarity(tk, meta["tokens"])
        if score > best_score:
            best, best_score = path, score
    if best_score < threshold:
        return "", best_score
    return best, best_score


def fetch_fallback_transcript(path: str, expected_title: str, timeout: int, sha: str = "", tries: int = 3) -> str:
    """优先用 Blob API 按 SHA 取内容（避免 Unicode 路径编码导致的 404），失败再用 raw 链接。"""
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    urls = []
    if sha:
        urls.append(f"https://api.github.com/repos/{FALLBACK_REPO}/git/blobs/{sha}")
    urls.append(RAW_BASE + quote(path, safe="/&"))
    last = "unknown"
    for attempt in range(tries):
        url = urls[attempt % len(urls)]
        try:
            r = requests.get(url, headers=headers if "api.github.com" in url else {"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                if "api.github.com" in url:
                    payload = r.json()
                    if payload.get("encoding") == "base64":
                        text = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
                    else:
                        text = payload.get("content", "")
                else:
                    text = r.text
                text = re.sub(r"\s+", " ", text).strip()
                words = len(text.split())
                if words < 400:
                    raise TranscriptError(f"fallback transcript too short: {words}")
                # 字幕稿无标点，做一次轻量还原：句首大写、补句号
                text = restore_punctuation(text)
                return text
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)[:100]
        time.sleep(2 + attempt * 4)
    raise TranscriptError(f"fallback fetch failed: {last}")


def restore_punctuation(text: str) -> str:
    """把无标点的字幕稿做最小可读化处理，便于后续翻译分段。"""
    filler = {
        " um ": " ", " uh ": " ", " ah ": " ", " you know ": ", ", " i mean ": ", ",
        " right? ": "? ", " okay? ": "? ", " ok? ": "? ", " yeah ": " ",
    }
    t = f" {text} "
    for a, b in filler.items():
        t = t.replace(a, b)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(music|applause|laughter)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    # 句号切分提示：每 ~180 个字符处插入句号（仅用于分段，不改变语义）
    parts = []
    buf = []
    size = 0
    for w in t.split():
        buf.append(w)
        size += len(w) + 1
        if size >= 180:
            parts.append(" ".join(buf))
            buf, size = [], 0
    if buf:
        parts.append(" ".join(buf))
    out = []
    for p in parts:
        p = p.strip()
        p = p[0].upper() + p[1:] if p else p
        out.append(p if p.endswith((".", "?", "!")) else p + ".")
    return " ".join(out)
