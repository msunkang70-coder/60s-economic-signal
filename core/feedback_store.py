"""
core/feedback_store.py
Fake Door 피드백 저장 — data/feedback.json 기반

원자적 쓰기 (tmp -> fsync -> os.replace) 패턴은 content_manager.py와 동일.
"""

import json
import os
import pathlib
import tempfile
from collections import Counter
from datetime import datetime

_ROOT = pathlib.Path(__file__).parent.parent
FEEDBACK_PATH = _ROOT / "data" / "feedback.json"


def _load_raw() -> list:
    if not FEEDBACK_PATH.exists():
        return []
    try:
        data = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_atomic(records: list) -> None:
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(records, ensure_ascii=False, indent=2)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=FEEDBACK_PATH.parent,
        prefix=".feedback_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, FEEDBACK_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_feedback(industry: str, would_use: str, free_text: str) -> dict:
    """피드백 1건을 data/feedback.json에 append."""
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industry": industry,
        "would_use": would_use,
        "free_text": free_text.strip(),
    }
    records = _load_raw()
    records.append(record)
    _write_atomic(records)
    return record


def load_feedback_summary() -> dict:
    """산업별 응답 집계 반환.

    Returns:
        {
            "total": int,
            "by_industry": {"반도체": {"count": 3, "would_use": {"예": 2, "아니오": 1}}, ...},
        }
    """
    records = _load_raw()
    by_industry: dict = {}
    for r in records:
        ind = r.get("industry", "일반")
        if ind not in by_industry:
            by_industry[ind] = {"count": 0, "would_use": Counter()}
        by_industry[ind]["count"] += 1
        by_industry[ind]["would_use"][r.get("would_use", "")] += 1

    # Counter -> dict for JSON serialization
    for v in by_industry.values():
        v["would_use"] = dict(v["would_use"])

    return {"total": len(records), "by_industry": by_industry}
