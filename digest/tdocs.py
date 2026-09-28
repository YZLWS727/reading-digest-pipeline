import hashlib
import json
import re
import time
from pathlib import Path

import requests

from .util import log


class TdocsError(RuntimeError):
    pass


class FatalTdocsError(TdocsError):
    """Token 失效/权限问题：停止本轮，不反复重试。"""


def safe_filename(title: str, limit: int = 60) -> str:
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:limit].strip() or "digest"


class TencentDocs:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.headers = {
            "Authorization": cfg.tdocs_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.ident = 0
        self._initialize()

    def _initialize(self) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "reading-digest", "version": "1.0"},
            },
        }
        r = self.session.post(self.cfg.tdocs_mcp_url, headers=self.headers, json=body, timeout=90)
        if r.status_code in (401, 403):
            raise FatalTdocsError(f"mcp init unauthorized {r.status_code}")
        if r.status_code != 200:
            raise FatalTdocsError(f"mcp init failed {r.status_code}: {r.text[:160]}")
        self.session.post(
            self.cfg.tdocs_mcp_url,
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=60,
        )
        self.ident = 1

    def call(self, tool: str, args: dict) -> dict:
        self.ident += 1
        body = {
            "jsonrpc": "2.0",
            "id": self.ident,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        r = self.session.post(self.cfg.tdocs_mcp_url, headers=self.headers, json=body, timeout=180)
        if r.status_code in (401, 403):
            raise FatalTdocsError(f"mcp call unauthorized {r.status_code}")
        payloads = []
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    val = line[5:].strip()
                    if val and val != "[DONE]":
                        try:
                            payloads.append(json.loads(val))
                        except Exception:  # noqa: BLE001
                            pass
        else:
            try:
                payloads.append(r.json())
            except Exception:  # noqa: BLE001
                raise TdocsError(f"bad response: {r.text[:160]}")
        text = ""
        for p in payloads:
            res = (p or {}).get("result") or {}
            if (p or {}).get("error"):
                raise TdocsError(f"tool error: {json.dumps(p['error'], ensure_ascii=False)[:200]}")
            for item in res.get("content", []) or []:
                if item.get("type") == "text":
                    text = item.get("text", "")
        if not text:
            raise TdocsError("empty tool result")
        try:
            return json.loads(text)
        except Exception:
            return {"_raw": text}

    def upload_docx(self, path: Path, display_title: str) -> dict:
        name = safe_filename(display_title) + ".docx"
        data = path.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        pre = self.call("manage.pre_import", {"file_name": name, "file_size": len(data), "file_md5": md5})
        upload_url = pre.get("upload_url")
        if not upload_url:
            raise TdocsError(f"pre_import failed: {json.dumps(pre, ensure_ascii=False)[:200]}")
        put = requests.put(
            upload_url,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=300,
        )
        if put.status_code not in (200, 201, 204):
            raise TdocsError(f"cos put failed {put.status_code}")
        self.call(
            "manage.async_import",
            {
                "file_key": pre.get("file_key"),
                "file_name": name,
                "file_md5": md5,
                "file_size": len(data),
                "task_id": pre.get("task_id"),
            },
        )
        task_id = pre.get("task_id")
        file_id = ""
        file_url = ""
        for i in range(45):
            time.sleep(4)
            prog = self.call("manage.import_progress", {"task_id": task_id})
            if str(prog.get("progress")) == "100":
                file_id = prog.get("file_id") or ""
                file_url = prog.get("file_url") or ""
                break
            if prog.get("error"):
                raise TdocsError(f"import error: {str(prog.get('error'))[:160]}")
        if not file_id:
            raise TdocsError("import timeout")
        self.call("manage.move_file", {"file_id": file_id, "target_folder_id": self.cfg.target_folder_id})
        info = self.call("manage.query_file_info", {"file_id": file_id})
        if info.get("title") is None and "_raw" not in info:
            raise TdocsError("verify failed: no title in file info")
        log("upload ok", f"doc_id={file_id}")
        return {"file_id": file_id, "file_url": file_url, "name": name}
