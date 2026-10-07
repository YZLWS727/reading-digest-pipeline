"""YouTube 官方字幕来源：yt-dlp 搜视频 + 抓字幕（第三来源，用于主源/备用源都没有的期）。"""

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .util import log, similarity, tokens

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class YouTubeError(RuntimeError):
    pass


def base_cmd() -> list[str]:
    """优先用 PATH 上的 yt-dlp，否则退回 python -m yt_dlp（pip --target 安装场景）。"""
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    return [sys.executable, "-m", "yt_dlp"]


def _run(args: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        [*base_cmd(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def available() -> bool:
    try:
        code, out, _ = _run(["--version"], 60)
        return code == 0 and bool(out.strip())
    except Exception:  # noqa: BLE001
        return False


def search_video(title: str, timeout: int = 180, min_score: float = 0.7) -> tuple[str, str, float]:
    """按标题搜索频道视频，返回 (video_id, 标题, 相似度)。"""
    query = f"ytsearch3:huberman lab {title}"
    args = [
        query,
        "--skip-download",
        "--no-warnings",
        "--print",
        "%(id)s\t%(title)s\t%(channel)s",
        "--user-agent",
        USER_AGENT,
    ]
    code, out, err = _run(args, timeout)
    if code != 0:
        raise YouTubeError(f"search failed rc={code} {err.strip()[:120]}")
    tk = tokens(title)
    best = ("", "", 0.0)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid, vtitle = parts[0].strip(), parts[1].strip()
        channel = parts[2].strip() if len(parts) > 2 else ""
        if channel and "huberman" not in channel.lower():
            continue
        score = similarity(tk, tokens(vtitle))
        if score > best[2]:
            best = (vid, vtitle, score)
    if best[2] < min_score:
        raise YouTubeError(f"no confident match (best={best[2]:.2f})")
    return best


def fetch_captions(video_id: str, workdir: Path, timeout: int = 300, langs: str = "en.*,en", proxy: str = "") -> str:
    """下载英文字幕并转成纯文本（自动字幕先去重）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    pattern = str(workdir / f"{video_id}.%(ext)s")
    args = [
        f"https://www.youtube.com/watch?v={video_id}",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        langs,
        "--sub-format",
        "vtt",
        "--convert-subs",
        "vtt",
        "--no-warnings",
        "--user-agent",
        USER_AGENT,
        "-o",
        pattern,
    ]
    if proxy:
        args += ["--proxy", proxy]
    code, out, err = _run(args, timeout)
    files = sorted(workdir.glob(f"{video_id}*.vtt"))
    if not files:
        raise YouTubeError(f"no caption file (rc={code}) {err.strip()[:150]}")
    text = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
    words = len(text.split())
    if words < 400:
        raise YouTubeError(f"captions too short ({words} words)")
    return text


def vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&nbsp;?", " ", line).strip()
        if not line:
            continue
        if lines and (line == lines[-1] or line in lines[-1] or lines[-1] in line):
            # 自动字幕存在滚动重复，保留更长的那条
            if len(line) > len(lines[-1]):
                lines[-1] = line
            continue
        lines.append(line)
    text = " ".join(lines)
    return re.sub(r"\s+", " ", text).strip()


def ensure_available() -> None:
    if not available():
        raise YouTubeError("yt-dlp not available")


def main_probe() -> int:
    """云端探针：只打印状态与计数，不打印任何标题/正文。"""
    print("yt-dlp available:", available(), flush=True)
    if not available():
        return 1
    try:
        code, out, err = _run(
            [
                "https://www.youtube.com/@hubermanlab/videos",
                "--flat-playlist",
                "--no-warnings",
                "--print",
                "%(id)s",
                "--playlist-end",
                "5",
                "--user-agent",
                USER_AGENT,
            ],
            300,
        )
        ids = [l.strip() for l in out.splitlines() if l.strip()]
        print(f"channel probe rc={code} ids={len(ids)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("channel probe failed:", repr(exc)[:120], flush=True)
        return 2
    if not ids:
        return 3
    vid = ids[0]
    try:
        text = fetch_captions(vid, Path("work/yt"), timeout=300)
        print(f"caption probe ok: id_len={len(vid)} words={len(text.split())}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("caption probe failed:", repr(exc)[:160], flush=True)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main_probe())
