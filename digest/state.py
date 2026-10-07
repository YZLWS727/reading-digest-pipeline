import json
from pathlib import Path


def load_state(path: Path) -> dict:
    if not Path(path).exists():
        return {"version": 1, "items": {}, "runs": []}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"version": 1, "items": {}, "runs": []}
    data.setdefault("items", {})
    data.setdefault("runs", [])
    return data
