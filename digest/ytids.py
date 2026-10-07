"""云端探针：为缺口期找 YouTube 视频 ID，并检查公开字幕数据集是否覆盖（只打印状态，不打印标题正文）。"""

import json
import os
from pathlib import Path

import requests

from .feed import build_queue, fetch_episodes
from .state import load_state
from .youtube import YouTubeError, search_video

CAPTION_REPOS = ["emptycloud-peak/huberman-transcripts"]


def repo_file_index(token: str) -> set[str]:
    files: set[str] = set()
    for repo in CAPTION_REPOS:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        info = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=60).json()
        branch = info.get("default_branch", "main")
        tree = requests.get(
            f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1", headers=headers, timeout=120
        ).json()
        for node in tree.get("tree", []):
            if node.get("type") == "blob":
                files.add(node["path"])
    return files


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    state = load_state(root / "state" / "state.json")
    items = state.get("items", {})
    feed_url = os.environ.get("FEED_URL", "").strip()
    if not feed_url:
        print("FEED_URL missing")
        return 2
    queue = build_queue(fetch_episodes(feed_url, 90))
    missing = [e for e in queue if items.get(e.key, {}).get("status") != "done"]
    print(f"missing={len(missing)}")

    files = repo_file_index(os.environ.get("GH_TOKEN", ""))
    print(f"caption repo files={len(files)}")

    covered = 0
    found = 0
    for ep in missing:
        try:
            vid, _title, score = search_video(ep.title, timeout=180, min_score=0.7)
            found += 1
        except YouTubeError as exc:
            print(f"  {ep.key} {ep.date} search_fail {str(exc)[:50]}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  {ep.key} {ep.date} search_error {repr(exc)[:50]}")
            continue
        has = f"{vid}.en.md" in files or f"{vid}.en.txt" in files or f"{vid}.txt" in files
        covered += 1 if has else 0
        print(f"  {ep.key} {ep.date} id_found=1 score={score:.2f} caption_repo={'yes' if has else 'no'}")
    print(f"summary: id_found={found}/{len(missing)} repo_covered={covered}/{len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
