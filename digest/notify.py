import json

import requests

from .util import log


def send(webhook: str, text: str) -> bool:
    if not webhook:
        log("notify skipped: no webhook")
        return False
    body = text if len(text.encode("utf-8")) <= 2000 else text.encode("utf-8")[:1900].decode("utf-8", "ignore") + "…"
    try:
        r = requests.post(webhook, json={"msgtype": "text", "text": {"content": body}}, timeout=30)
        ok = r.status_code == 200 and '"errcode":0' in r.text.replace(" ", "")
        log("notify", "ok" if ok else f"failed {r.status_code}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log("notify error", repr(exc)[:120])
        return False
