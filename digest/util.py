import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))


def log(*parts: object) -> None:
    stamp = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] " + " ".join(str(p) for p in parts), flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def item_key(guid: str) -> str:
    return hashlib.sha1((guid or "").encode("utf-8")).hexdigest()[:16]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def strip_html(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = (
        t.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", t).strip()


STOP = {
    "a", "an", "the", "of", "for", "with", "and", "to", "in", "on", "your", "you",
    "how", "is", "are", "it", "that", "this", "at", "by", "from", "as", "be", "dr",
}


def tokens(text: str) -> set[str]:
    t = text.lower()
    for a, b in [("ø", "o"), ("é", "e"), ("á", "a"), ("í", "i"), ("ö", "o"), ("ü", "u"), ("ç", "c"), ("ñ", "n"), ("ș", "s"), ("ț", "t")]:
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return {w for w in t.split() if w and w not in STOP}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text))


def ascii_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{2,}", text))


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def chunk_sentences(sentences: list[str], max_words: int, max_chars: int = 6000) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    words = 0
    chars = 0
    for s in sentences:
        sw = len(s.split())
        if cur and (words + sw > max_words or chars + len(s) > max_chars):
            chunks.append(" ".join(cur))
            cur, words, chars = [], 0, 0
        cur.append(s)
        words += sw
        chars += len(s) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def commit_state(repo_root: Path, message: str) -> bool:
    code, out = run_git(["add", "state/state.json", "state/progress.md"], repo_root)
    if code != 0:
        log("git add failed", out.strip()[:200])
        return False
    code, out = run_git(["diff", "--cached", "--quiet"], repo_root)
    if code == 0:
        return True
    code, out = run_git(["commit", "-m", message], repo_root)
    if code != 0:
        log("git commit failed", out.strip()[:200])
        return False
    for attempt in range(3):
        code, out = run_git(["pull", "--rebase", "--autostash", "origin", "HEAD"], repo_root)
        code2, out2 = run_git(["push"], repo_root)
        if code2 == 0:
            return True
        log("git push retry", attempt + 1, out2.strip()[:200])
        time.sleep(4 + attempt * 6)
    return False


def sleep_backoff(attempt: int, base: float = 3.0, cap: float = 45.0) -> None:
    time.sleep(min(cap, base * (2 ** max(0, attempt))))
