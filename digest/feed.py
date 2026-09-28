import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .util import item_key, log, strip_html

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# 2025 年前按主题分批处理（编号 = 2025 年前按期排序后的序号），顺序即处理优先级
PRE_GROUPS: list[tuple[str, list[int]]] = [
    ("A", [2, 7, 8, 9, 21, 38, 47, 51, 59, 73, 74, 81, 89, 91, 92, 98, 110, 115, 128, 137, 143, 145, 151, 152, 165, 186, 192, 196, 200, 201, 203, 208, 236, 241, 249]),
    ("B", [11, 13, 19, 36, 40, 54, 56, 57, 84, 111, 133, 153, 159, 171, 180, 188, 206]),
    ("C", [3, 4, 5, 6, 32, 44, 69, 85, 123, 205, 207, 209, 212, 215, 217]),
    ("D", [105, 147, 155, 182, 218, 243, 253, 257, 261]),
    ("E", [20, 41, 55, 67, 70, 125, 141]),
    ("F", [30, 136, 161, 167, 173, 224]),
    ("G", [45, 62, 63, 189, 221]),
]

RECENT_FROM = "2025-01-01"


@dataclass
class Episode:
    key: str
    date: str
    title: str
    guest: str
    minutes: int
    link: str
    guid: str
    group: str

    @property
    def core_title(self) -> str:
        return extract_core(self.title)


def extract_core(title: str) -> str:
    t = re.sub(r"\s*\|\s*[^|]{2,80}$", "", title).strip()
    return t or title.strip()


def extract_guest(title: str) -> str:
    m = re.search(r"\|\s*([^|]{2,60})\s*$", title)
    if not m:
        return ""
    guest = m.group(1).strip()
    if len(guest.split()) > 6:
        return ""
    return guest


def _dedup_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", (raw or "").strip())
    half = len(t) // 2
    if half > 10 and t[:half].strip().lower() == t[half:].strip().lower():
        return t[:half].strip()
    return t


def fetch_episodes(feed_url: str, timeout: int = 90) -> list[Episode]:
    r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns_itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    items = root.findall("./channel/item")
    episodes: list[Episode] = []
    for it in items:
        title = _dedup_title(it.findtext("title") or "")
        guid = (it.findtext("guid") or title).strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        dur = (it.findtext(f"{ns_itunes}duration") or "").strip()
        try:
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(timezone.utc)
        except Exception:
            try:
                dt = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
        minutes = int(dur) // 60 if dur.isdigit() else 0
        episodes.append(
            Episode(
                key=item_key(guid),
                date=dt.strftime("%Y-%m-%d"),
                title=title,
                guest=extract_guest(title),
                minutes=minutes,
                link=link,
                guid=guid,
                group="",
            )
        )
    episodes.sort(key=lambda e: e.date)
    log("feed parsed", f"items={len(episodes)}", f"oldest={episodes[0].date if episodes else '-'}", f"newest={episodes[-1].date if episodes else '-'}")
    return episodes


def build_queue(episodes: list[Episode]) -> list[Episode]:
    """新内容优先（近→远），其后按主题分组顺序处理 2025 年前内容。"""
    recent = [e for e in episodes if e.date >= RECENT_FROM]
    recent.sort(key=lambda e: e.date, reverse=True)
    for e in recent:
        e.group = "R"

    older = [e for e in episodes if e.date < RECENT_FROM]
    older.sort(key=lambda e: e.date)  # 与分类时的编号口径一致（升序）
    queue: list[Episode] = list(recent)
    index = {i + 1: e for i, e in enumerate(older)}
    for group, ids in PRE_GROUPS:
        picked = [index[i] for i in sorted(ids, reverse=True) if i in index]
        for e in picked:
            e.group = group
        queue.extend(picked)
    log("queue built", f"recent={len(recent)}", f"older_selected={sum(len(ids) for _, ids in PRE_GROUPS)}", f"total={len(queue)}")
    return queue
