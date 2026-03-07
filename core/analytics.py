"""
core/analytics.py
사용자 이벤트 로깅 — data/user_logs/ 일별 JSON.

이벤트 종류: page_view, article_click, industry_select, feedback_submit 등.
User Test 분석용.
"""

import json
import os
import pathlib
import tempfile
from datetime import datetime

_ROOT = pathlib.Path(__file__).parent.parent
_LOG_DIR = _ROOT / "data" / "user_logs"


def _today_path() -> pathlib.Path:
    return _LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"


def _load_today() -> list:
    path = _today_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_atomic(records: list) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _today_path()
    payload = json.dumps(records, ensure_ascii=False, indent=2)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=_LOG_DIR,
        prefix=".log_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def log_event(
    event_type: str,
    detail: dict | None = None,
) -> None:
    """이벤트 1건을 오늘자 로그 파일에 append.

    Args:
        event_type: "page_view", "article_click", "industry_select",
                    "feedback_submit", "macro_refresh", "report_download" 등
        detail: 추가 정보 dict (예: {"doc_id": "...", "title": "..."})
    """
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event_type,
    }
    if detail:
        record["detail"] = detail

    records = _load_today()
    records.append(record)
    try:
        _write_atomic(records)
    except Exception as e:
        print(f"[analytics] 로그 저장 실패: {e}")


def get_today_events(event_type: str | None = None) -> list:
    """오늘자 이벤트 조회. event_type 지정 시 필터링."""
    records = _load_today()
    if event_type:
        return [r for r in records if r.get("event") == event_type]
    return records
