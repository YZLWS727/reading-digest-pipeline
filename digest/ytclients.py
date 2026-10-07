"""探针：尝试多种 yt-dlp 客户端配置抓字幕，看哪种能绕过 bot 检测（只打印状态）。"""

import shutil
import subprocess
import sys
from pathlib import Path

from .youtube import USER_AGENT, vtt_to_text

YTDLP = shutil.which("yt-dlp") or "yt-dlp"
VIDEO = "EIhilBpn8Ow"
CLIENTS = [
    ("default", []),
    ("android", ["--extractor-args", "youtube:player_client=android"]),
    ("ios", ["--extractor-args", "youtube:player_client=ios"]),
    ("tv", ["--extractor-args", "youtube:player_client=tv"]),
    ("web_safari", ["--extractor-args", "youtube:player_client=web_safari"]),
    ("mweb", ["--extractor-args", "youtube:player_client=mweb"]),
]


def try_client(name: str, extra: list[str]) -> str:
    outdir = Path("work/yt") / name
    outdir.mkdir(parents=True, exist_ok=True)
    args = [
        YTDLP,
        f"https://www.youtube.com/watch?v={VIDEO}",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "en.*,en",
        "--sub-format",
        "vtt",
        "--no-warnings",
        "--user-agent",
        USER_AGENT,
        "-o",
        str(outdir / "%(id)s.%(ext)s"),
        *extra,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    except subprocess.TimeoutExpired:
        return "timeout"
    files = list(outdir.glob("*.vtt"))
    if files:
        text = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
        return f"OK words={len(text.split())}"
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = err[-1][:90] if err else "no output"
    return f"fail rc={proc.returncode} {tail}"


def main() -> int:
    ok = 0
    for name, extra in CLIENTS:
        result = try_client(name, extra)
        print(f"client={name:<11} {result}", flush=True)
        if result.startswith("OK"):
            ok += 1
    print(f"summary: working_clients={ok}/{len(CLIENTS)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
