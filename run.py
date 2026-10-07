"""Orchestrator: fetch → translate → typeset → sync, with resumable state.

设计要点：
- 状态机落盘（state/state.json），任何一步失败只重跑该步，跨天自动续跑；
- 每天仅在设定的北京时间窗口内处理新期（窗口内最后开始时的一期允许跑完）；
- 免费模型白名单硬约束，认证/余额类错误立即停机，绝不切付费模型；
- 全流程不把正文写入日志，仓库只保存哈希与计数。
"""

import argparse
import json
import os
import time
import requests
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from digest.config import Config
from digest.feed import Episode, build_queue, fetch_episodes
from digest.notify import send
from digest.render import build_docx, paragraphs_from_text
from digest.tdocs import FatalTdocsError, TencentDocs
from digest.transcript import (
    TranscriptError,
    build_fallback_index,
    build_index,
    fetch_fallback_transcript,
    fetch_transcript,
    match_fallback,
    match_slug,
)
from digest.translate import FatalApiError, translate_transcript
from digest.util import (
    BEIJING,
    atomic_write_json,
    beijing_now,
    commit_state,
    log,
    read_json,
)

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "state.json"
PROGRESS_PATH = ROOT / "state" / "progress.md"
WORK_DIR = ROOT / "work"
FALLBACK_INDEX: dict[str, set[str]] = {}
FALLBACK_USED: set[str] = set()


class ItemError(RuntimeError):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def parse_hhmm(value: str, fallback: dtime) -> dtime:
    try:
        hh, mm = value.split(":")
        return dtime(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        return fallback


def compute_window(cfg: Config, force: bool) -> tuple[datetime | None, datetime | None, str]:
    """返回（可启动新期的截止时间, 进程硬截止时间, 说明）。None 表示本轮不处理。"""
    bj = beijing_now()
    start_t = parse_hhmm(cfg.daily_start_beijing, dtime(12, 0))
    cutoff_t = parse_hhmm(cfg.daily_cutoff_beijing, dtime(22, 30))
    start_bj = datetime.combine(bj.date(), start_t, tzinfo=BEIJING)
    cutoff_bj = datetime.combine(bj.date(), cutoff_t, tzinfo=BEIJING)
    hard = datetime.now(timezone.utc) + timedelta(minutes=cfg.max_job_minutes)
    if force:
        return hard, hard, "force"
    if bj < start_bj:
        return None, None, f"before window ({start_bj:%H:%M})"
    if bj >= cutoff_bj:
        return None, None, f"after cutoff ({cutoff_bj:%H:%M})"
    start_new = min(cutoff_bj.astimezone(timezone.utc), hard)
    return start_new, hard, f"window until {cutoff_bj:%H:%M} Beijing"


def item_budget_minutes(ep: Episode, cfg: Config) -> float:
    return min(60.0, max(12.0, 12.0 + ep.minutes / 8.0)) if ep.minutes else max(15.0, cfg.max_item_minutes)


def load_state() -> dict:
    state = read_json(STATE_PATH, {})
    state.setdefault("version", 1)
    state.setdefault("items", {})
    state.setdefault("runs", [])
    return state


def save_state(state: dict, cfg: Config, commit: bool) -> None:
    state["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atomic_write_json(STATE_PATH, state)
    if commit and os.environ.get("GIT_COMMIT") == "1":
        commit_state(ROOT, f"state: {sum(1 for v in state['items'].values() if v.get('status') == 'done')} done")


def append_progress(done_today: int, done_total: int, remaining: int, note: str) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PROGRESS_PATH.exists():
        PROGRESS_PATH.write_text(
            "# Progress\n\n| 时间(北京) | 本轮完成 | 累计完成 | 剩余 | 备注 |\n| --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
    stamp = beijing_now().strftime("%Y-%m-%d %H:%M")
    with PROGRESS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"| {stamp} | {done_today} | {done_total} | {remaining} | {note} |\n")


def process_item(cfg: Config, ep: Episode, state: dict, index: dict, used: set[str], tdocs: TencentDocs | None, dry_run: bool) -> dict:
    global FALLBACK_INDEX, FALLBACK_USED
    record = state["items"].setdefault(ep.key, {"date": ep.date, "status": "pending", "attempts": 0})
    budget = item_budget_minutes(ep, cfg)
    started = time.time()

    def _save() -> None:
        if not dry_run:
            save_state(state, cfg, commit=True)

    record["last_try"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record["attempts"] = int(record.get("attempts") or 0) + 1

    source = record.get("source") or ""
    ref = record.get("slug") or record.get("fallback_path") or ""
    if not ref:
        slug, score = match_slug(index, ep.title, used)
        if slug:
            source, ref = "site", slug
            record["slug"] = slug
            record["match_score"] = round(score, 2)
        else:
            path, fscore = match_fallback(FALLBACK_INDEX, ep.title, FALLBACK_USED)
            if not path:
                raise ItemError(f"no transcript match (site={score:.2f} fallback={fscore:.2f})", kind="no_transcript")
            source, ref = "github", path
            record["fallback_path"] = path
            record["match_score"] = round(fscore, 2)
        record["source"] = source
    if source == "site":
        used.add(ref)
    else:
        FALLBACK_USED.add(ref)
    log("item", f"key={ep.key}", f"date={ep.date}", f"group={ep.group}", f"src={source}", f"match={record.get('match_score')}")

    cache = WORK_DIR / f"{ep.key}.txt"
    if cache.exists() and cache.stat().st_size > 2000:
        text = cache.read_text(encoding="utf-8")
    elif source == "site":
        try:
            text = fetch_transcript(
                f"{urlparse(cfg.transcript_base).scheme}://{urlparse(cfg.transcript_base).netloc}",
                ref,
                ep.title,
                cfg.request_timeout,
            )
        except TranscriptError as exc:
            if "title mismatch" not in str(exc):
                raise
            # 主源匹配到的页面不是这一期：改用备用源，避免反复撞同一页
            log("title mismatch -> fallback", f"key={ep.key}")
            record.pop("slug", None)
            path, fscore = match_fallback(FALLBACK_INDEX, ep.title, FALLBACK_USED)
            if not path:
                record["source"] = ""
                raise ItemError(f"title mismatch and no fallback (best={fscore:.2f})", kind="no_transcript")
            record["fallback_path"] = path
            record["source"] = "github"
            record["match_score"] = round(fscore, 2)
            FALLBACK_USED.add(path)
            text = fetch_fallback_transcript(path, ep.title, cfg.request_timeout)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    else:
        text = fetch_fallback_transcript(ref, ep.title, cfg.request_timeout)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    record["words"] = len(text.split())
    record["status"] = "fetched"
    _save()
    log("fetched", f"words={record['words']}")

    if time.time() - started > budget * 60:
        raise ItemError("budget exceeded after fetch", kind="timeout")

    result = translate_transcript(cfg, text, ep.title)
    paragraphs = paragraphs_from_text(result["body"])
    record["chars"] = result["chars"]
    record["cjk"] = result["cjk"]
    record["chunks"] = result["chunks"]
    record["failed_chunks"] = result.get("failed_chunks", 0)
    record["title_zh"] = result["title_zh"] or ep.core_title
    record["status"] = "translated"
    _save()

    if time.time() - started > budget * 60:
        raise ItemError("budget exceeded after translate", kind="timeout")

    display = f"{ep.date} {record['title_zh']}"
    meta = {
        "title_zh": record["title_zh"],
        "meta_line": f"{ep.date} ｜ 时长约 {ep.minutes} 分钟" + (f" ｜ 嘉宾 {ep.guest}" if ep.guest else ""),
        "orig_title": f"原文：{ep.title}",
        "note": (
            "本稿由 AI 依据公开英文逐字稿翻译整理，仅供个人学习参考；专业术语与数据如与英文原文有出入，请以原文为准。"
            + (f"\n节目页面：{ep.link}" if ep.link else "")
        ),
    }
    docx_path = WORK_DIR / f"{ep.key}.docx"
    build_docx(docx_path, meta, paragraphs)
    record["status"] = "rendered"
    _save()

    if dry_run:
        record["status"] = "done"
        record["doc_id"] = "DRY_RUN"
        record["doc_url"] = ""
        log("dry-run done", f"key={ep.key}", f"chars={record['chars']}")
        return record

    if tdocs is None:
        raise ItemError("no tdocs client", kind="internal")
    up = tdocs.upload_docx(docx_path, display)
    record["doc_id"] = up["file_id"]
    record["doc_url"] = up["file_url"]
    record["status"] = "done"
    record["done_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save()
    log("item done", f"key={ep.key}", f"chars={record['chars']}", f"took={int(time.time()-started)}s")
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本轮最多处理多少期（0=不限）")
    ap.add_argument("--only", default="", help="只处理指定 key")
    ap.add_argument("--dry-run", action="store_true", help="不写状态、不上传")
    ap.add_argument("--force", action="store_true", help="忽略时间窗限制（手动补跑用）")
    ap.add_argument("--no-commit", action="store_true", help="不执行 git 提交")
    ap.add_argument("--chain", type=int, default=0, help="自链接深度（防止无限循环）")
    ap.add_argument("--no-cooldown", action="store_true", help="忽略失败冷却，立刻重试全部剩余期")
    args = ap.parse_args()

    cfg = Config()
    problems = cfg.validate(need_upload=not args.dry_run)
    if problems:
        log("config error", "; ".join(problems))
        return 2
    if args.no_commit:
        os.environ["GIT_COMMIT"] = "0"

    start_new_deadline, hard_deadline, why = compute_window(cfg, args.force)
    if start_new_deadline is None:
        log("skip run", why)
        return 0
    log("run start", why, f"max_new_until={start_new_deadline.astimezone(BEIJING):%H:%M}", f"hard_stop={hard_deadline.astimezone(BEIJING):%H:%M}")

    state = load_state()
    episodes = fetch_episodes(cfg.feed_url, cfg.request_timeout)
    queue = build_queue(episodes)
    done_total = sum(1 for v in state["items"].values() if v.get("status") == "done")
    targets = {e.key for e in queue}
    pending = [e for e in queue if state["items"].get(e.key, {}).get("status") != "done"]
    log("state", f"queue={len(queue)}", f"done={done_total}", f"remaining={len(pending)}", f"next={pending[0].key if pending else '-'}")

    # 停摆看门狗：超过 30 小时没有任何新产出就主动告警
    done_stamps = [v.get("done_at") for v in state["items"].values() if v.get("status") == "done" and v.get("done_at")]
    if done_stamps:
        newest = max(done_stamps)
        try:
            gap_h = (datetime.now(timezone.utc) - datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            gap_h = 0.0
        if gap_h > 30 and pending:
            log("stall watchdog", f"gap_hours={gap_h:.1f}")
            send(cfg.wecom_webhook, f"阅读专栏：已连续 {gap_h:.0f} 小时没有新产出（剩余 {len(pending)} 期），请检查 GitHub Actions 定时任务是否被延迟或禁用。")

    if args.only:
        pending = [e for e in pending if e.key == args.only]
    if args.limit:
        pending = pending[: args.limit]

    # 反复缺稿的期不每轮重试：20 小时内只尝试一次，避免浪费作业时间
    def _cooling(v: dict) -> bool:
        err = str(v.get("last_error") or "")
        tries = int(v.get("attempts") or 0)
        if v.get("status") != "blocked" and tries < 3 and "no_transcript" not in err:
            return False
        ts = v.get("last_error_at") or ""
        try:
            last = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return False
        return (datetime.now(timezone.utc) - last) < timedelta(hours=20)

    cooling = [] if args.no_cooldown else [e for e in pending if _cooling(state["items"].get(e.key, {}))]
    if cooling:
        pending = [e for e in pending if e not in cooling]
        log("cooldown skip", f"count={len(cooling)}")
    if not pending:
        log("nothing to do")
        msg = f"阅读专栏：没有可处理的期数（剩余 {len(cooling)} 期因暂时无文字稿进入冷却，稍后自动重试）。" if cooling else f"阅读专栏：全部 {len(queue)} 期已完成。"
        send(cfg.wecom_webhook, msg)
        return 0

    index = build_index(cfg.transcript_base, cfg.request_timeout)
    used = {v.get("slug") for v in state["items"].values() if v.get("slug")}
    FALLBACK_INDEX.update(build_fallback_index(os.environ.get("GH_TOKEN", ""), cfg.request_timeout))
    FALLBACK_USED.update({v.get("fallback_path") for v in state["items"].values() if v.get("fallback_path")})

    tdocs = None
    if not args.dry_run:
        try:
            tdocs = TencentDocs(cfg)
        except FatalTdocsError as exc:
            log("tdocs fatal", str(exc)[:160])
            send(cfg.wecom_webhook, f"阅读专栏：云文档 Token 异常，已停止本轮。原因：{str(exc)[:80]}")
            return 3

    done_now = 0
    failures: list[tuple[str, str]] = []
    for ep in pending:
        now = datetime.now(timezone.utc)
        if now >= start_new_deadline:
            log("stop starting new items", f"now={now.astimezone(BEIJING):%H:%M}")
            break
        if now >= hard_deadline:
            log("job hard stop")
            break
        try:
            process_item(cfg, ep, state, index, used, tdocs, args.dry_run)
            done_now += 1
            done_total += 1
        except FatalApiError as exc:
            log("fatal api error", str(exc)[:160])
            failures.append((ep.key, "fatal_api"))
            send(cfg.wecom_webhook, f"阅读专栏：翻译接口异常已停机（不切付费模型）。原因：{str(exc)[:100]}")
            break
        except (ItemError, TranscriptError, RuntimeError, Exception) as exc:  # noqa: BLE001
            kind = getattr(exc, "kind", "error")
            rec = state["items"].setdefault(ep.key, {"date": ep.date, "status": "pending", "attempts": 0})
            rec["last_error"] = f"{kind}: {str(exc)[:180]}"
            rec["last_error_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if kind == "no_transcript":
                rec["status"] = "pending"
            if int(rec.get("attempts") or 0) >= 8:
                rec["status"] = "blocked"
            failures.append((ep.key, kind))
            log("item failed", f"key={ep.key}", f"kind={kind}")
            save_state(state, cfg, commit=True)

    remaining = len([e for e in queue if state["items"].get(e.key, {}).get("status") != "done"])
    append_progress(done_now, done_total, remaining, f"failures={len(failures)}")
    save_state(state, cfg, commit=not args.dry_run)
    log("run end", f"done_now={done_now}", f"done_total={done_total}", f"remaining={remaining}", f"failures={len(failures)}")

    # 自链接：本轮因作业时长上限停下、但时间窗未结束时，自动再触发一轮，避免依赖 GitHub 定时准点
    chain_ok = False
    bj_now = datetime.now(BEIJING)
    cutoff_today = datetime.combine(bj_now.date(), parse_hhmm(cfg.daily_cutoff_beijing, dtime(22, 30)), tzinfo=BEIJING)
    token = os.environ.get("GH_TOKEN", "")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")
    if token and repo_slug and remaining > 0 and done_now > 0 and args.chain < 4 and (cutoff_today - bj_now) > timedelta(minutes=25):
        try:
            resp = requests.post(
                f"https://api.github.com/repos/{repo_slug}/actions/workflows/sync.yml/dispatches",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                json={"ref": "main", "inputs": {"limit": "0", "force": "false", "only": "", "chain": str(args.chain + 1)}},
                timeout=60,
            )
            chain_ok = resp.status_code < 300
            log("chain dispatch", resp.status_code, f"chain={args.chain + 1}")
        except Exception as exc:  # noqa: BLE001
            log("chain dispatch failed", repr(exc)[:120])

    summary = [f"阅读专栏：本轮完成 {done_now} 期，累计 {done_total} 期，剩余 {remaining} 期。"]
    if failures:
        summary.append("失败：" + "、".join(f"{k}({kind})" for k, kind in failures[:5]))
    if cooling:
        summary.append(f"暂无文字稿、稍后重试：{len(cooling)} 期。")
    if chain_ok:
        summary.append("已自动接力下一轮。")
    send(cfg.wecom_webhook, "\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
