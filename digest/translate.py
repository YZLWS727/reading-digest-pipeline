import json
import re
import time

import requests

from .config import Config
from .util import ascii_word_count, cjk_count, chunk_sentences, log, sleep_backoff, split_sentences


class TranslationError(RuntimeError):
    pass


class FatalApiError(TranslationError):
    """认证/余额/权限类错误：立刻停止，绝不切换到付费模型。"""


SYSTEM_PROMPT = (
    "你是一名专业的科学播客文字稿译者，负责把英文播客逐字稿翻译成严谨、流畅的简体中文。"
)

CHUNK_PROMPT = """请把下面的英文播客逐字稿翻译成简体中文，要求：
1. 严谨忠实：不增删信息、不总结、不评论、不加标题、不输出原文；
2. 术语准确：神经科学、生理学、营养学等术语使用规范译名，首次出现时用「中文（English）」标注，其后只用中文或通用缩写；
3. 保留专有名词：人名、机构名、产品名、期刊名保留英文原文；
4. 数字与单位原样保留（剂量、时长、温度、百分比等）；
5. 逐字稿中明显的语音识别错误（如把专业术语听错拼错、人名错拼）请按上下文纠正为正确写法；
6. 口语化表达转成自然的中文书面语，但不要改变说话人的语气与意思；
7. 只输出译文本身。

英文逐字稿：
"""

TITLE_PROMPT = """把下面这期播客的英文标题翻译成简体中文标题，要求：
1. 简洁、准确、符合中文播客标题习惯，不超过 30 个汉字；
2. 保留人名英文原文（如 Dr. Andy Galpin）；
3. 标题中的 Essentials 一律译作「精华版」，AMA 保留为「AMA」；
4. 只输出标题本身，不要引号、不要解释、不要句号。

英文标题："""

REFUSAL_MARKERS = ("无法翻译", "抱歉，我", "I cannot", "I'm sorry", "作为一个 AI", "作为AI")
ENGLISH_RUN_RE = re.compile(r"(?:[A-Za-z][A-Za-z',\.\-]*\s+){25,}")
REPAIR_PROMPT = """下面是一篇中文译稿里漏译的英文句子。请逐条翻译成简体中文，要求：
1. 严格按编号输出，格式为「序号. 译文」；
2. 不要合并、不要遗漏、不要解释、不要输出英文原文；
3. 术语与上文译稿保持一致（神经科学、生理学、营养学）。

英文句子：
"""


class Translator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.headers = {
            "Authorization": f"Bearer {cfg.siliconflow_key}",
            "Content-Type": "application/json",
        }
        self.disabled: set[str] = set()
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def _post(self, model: str, prompt: str, max_tokens: int, temperature: float = 0.2) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if model.startswith("Qwen/Qwen3"):
            payload["enable_thinking"] = False
        r = requests.post(
            self.cfg.siliconflow_base.rstrip("/") + "/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=self.cfg.request_timeout,
        )
        self.usage["requests"] += 1
        if r.status_code in (401, 402, 403):
            raise FatalApiError(f"siliconflow auth/balance {r.status_code}: {r.text[:160]}")
        if r.status_code != 200:
            raise TranslationError(f"http {r.status_code}: {r.text[:160]}")
        body = r.json()
        usage = body.get("usage") or {}
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return content.strip()

    def _clean(self, text: str) -> str:
        t = text.strip()
        t = re.sub(r"^(译文[:：]|翻译[:：])\s*", "", t)
        t = t.strip().strip('"').strip("“”").strip()
        return t

    def translate_chunk(self, text: str) -> str:
        en_words = len(text.split())
        last_err = "unknown"
        for model in [m for m in self.cfg.models if m not in self.disabled]:
            for attempt in range(self.cfg.max_chunk_retries):
                try:
                    out = self._clean(self._post(model, CHUNK_PROMPT + text, max_tokens=4096))
                    problem = self._qa(out, en_words)
                    if problem:
                        last_err = f"{model} qa:{problem}"
                        sleep_backoff(attempt, 2.0, 20.0)
                        continue
                    return out
                except FatalApiError:
                    raise
                except TranslationError as exc:
                    last_err = f"{model} {exc}"
                    msg = str(exc)
                    if "20012" in msg or "model" in msg.lower() and "not" in msg.lower():
                        self.disabled.add(model)
                        log("model disabled", model)
                        break
                    sleep_backoff(attempt, 3.0, 30.0)
            log("model failed", model, last_err[:120])
        raise TranslationError(f"all models failed: {last_err}")

    @staticmethod
    def _qa(out: str, en_words: int) -> str:
        if not out:
            return "empty"
        if any(marker in out for marker in REFUSAL_MARKERS):
            return "refusal"
        cjk = cjk_count(out)
        ratio = cjk / max(1, en_words)
        if ratio < 0.6:
            return f"too_little_chinese ratio={ratio:.2f}"
        if ratio > 3.0:
            return f"too_long ratio={ratio:.2f}"
        if ascii_word_count(out) > en_words * 0.55:
            return "mostly_untranslated"
        if ENGLISH_RUN_RE.search(out):
            return "english_run"
        if len(out) > 12 and len(set(out)) / len(out) < 0.05:
            return "repetitive"
        return ""

    @staticmethod
    def _bad_sentences(text: str) -> list[str]:
        bad: list[str] = []
        for s in split_sentences(text):
            words = ascii_word_count(s)
            if words >= 8 and cjk_count(s) <= max(3, words // 3):
                bad.append(s.strip())
        return bad

    def repair_english(self, text: str, rounds: int = 2) -> tuple[str, int]:
        """把漏译的英文句子补译回中文（逐条编号，严格替换）。"""
        total_fixed = 0
        for _ in range(rounds):
            bad = self._bad_sentences(text)
            if not bad:
                break
            fixed_map: dict[str, str] = {}
            for start in range(0, len(bad), 12):
                batch = bad[start : start + 12]
                listing = "\n".join(f"{i+1}. {s}" for i, s in enumerate(batch))
                out = ""
                for model in [m for m in self.cfg.models if m not in self.disabled]:
                    try:
                        out = self._clean(self._post(model, REPAIR_PROMPT + listing, max_tokens=3000, temperature=0.2))
                        if cjk_count(out) > 20:
                            break
                    except FatalApiError:
                        raise
                    except TranslationError:
                        out = ""
                pairs = re.findall(r"(?m)^\s*(\d{1,2})\s*[.、)]\s*(.+?)\s*$", out)
                for num, zh in pairs:
                    idx = int(num) - 1
                    if 0 <= idx < len(batch) and cjk_count(zh) >= 4:
                        fixed_map[batch[idx]] = zh.strip()
            if not fixed_map:
                break
            for src, dst in fixed_map.items():
                text = text.replace(src, dst)
            total_fixed += len(fixed_map)
            log("repair pass", f"fixed={len(fixed_map)}")
        return text, total_fixed

    def translate_title(self, title: str) -> str:
        for model in [m for m in self.cfg.models if m not in self.disabled]:
            for attempt in range(2):
                try:
                    out = self._clean(self._post(model, TITLE_PROMPT + title, max_tokens=200, temperature=0.3))
                    out = out.splitlines()[0].strip().strip("。")
                    if 2 <= cjk_count(out) and len(out) <= 60:
                        return out
                except FatalApiError:
                    raise
                except TranslationError:
                    sleep_backoff(attempt, 2.0, 12.0)
        return ""


def translate_transcript(cfg: Config, transcript: str, title: str) -> dict:
    translator = Translator(cfg)
    chunks = chunk_sentences(split_sentences(transcript), cfg.chunk_words)
    log("translate start", f"chunks={len(chunks)}")
    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        out = translator.translate_chunk(chunk)
        parts.append(out)
        if i % 5 == 0 or i == len(chunks):
            log("translate progress", f"{i}/{len(chunks)}")
    body = "\n".join(parts)
    body, repaired = translator.repair_english(body)
    title_zh = translator.translate_title(title)
    log("translate done", f"chunks={len(chunks)}", f"chars={len(body)}", f"repaired={repaired}", f"api_requests={translator.usage['requests']}")
    return {
        "body": body,
        "title_zh": title_zh,
        "chunks": len(chunks),
        "chars": len(body),
        "cjk": cjk_count(body),
        "repaired": repaired,
        "usage": translator.usage,
    }
