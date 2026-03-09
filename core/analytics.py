"""
core/analytics.py
사용자 이벤트 로깅 — data/user_logs/ 일별 JSON.

이벤트 종류: page_view, article_click, industry_select, feedback_submit,
            email_click, report_download, macro_refresh, signal_view 등.
User Test 분석용.

로그 저장: data/user_logs/YYYY-MM-DD.json (일별 파일)
"""

import json
import os
import pathlib
import tempfile
from datetime import datetime, timedelta
from typing import Optional

_ROOT = pathlib.Path(__file__).parent.parent
_LOG_DIR = _ROOT / "data" / "user_logs"


def _today_path() -> pathlib.Path:
    return _LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"


def _date_path(date_str: str) -> pathlib.Path:
    return _LOG_DIR / f"{date_str}.json"


def _load_today() -> list:
    path = _today_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _load_date(date_str: str) -> list:
    """특정 일자의 로그를 로드한다."""
    path = _date_path(date_str)
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
                    "feedback_submit", "macro_refresh", "report_download",
                    "email_click", "signal_view" 등
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


def get_daily_summary(date_str: Optional[str] = None) -> dict:
    """
    특정 일자의 로그를 집계하여 요약 통계를 반환한다.

    Args:
        date_str: "YYYY-MM-DD" (None이면 오늘)

    Returns:
        {
            "date": str,
            "total_events": int,
            "page_views": int,
            "article_clicks": int,
            "report_downloads": int,
            "email_clicks": int,
            "feedback_submits": int,
            "industry_breakdown": {"반도체": 5, "자동차": 3, ...},
            "top_articles": [{"article_id": "...", "clicks": 3}, ...],
        }
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    entries = _load_date(date_str)
    if not entries:
        return {"date": date_str, "total_events": 0}

    summary: dict = {
        "date": date_str,
        "total_events": len(entries),
        "page_views": 0,
        "article_clicks": 0,
        "report_downloads": 0,
        "email_clicks": 0,
        "feedback_submits": 0,
        "industry_breakdown": {},
        "top_articles": [],
    }

    article_counts: dict[str, int] = {}

    for e in entries:
        et = e.get("event", "")
        detail = e.get("detail", {})
        ind = detail.get("industry", "")

        if et == "page_view":
            summary["page_views"] += 1
        elif et in ("article_click", "article_expand"):
            summary["article_clicks"] += 1
            aid = str(detail.get("doc_id", detail.get("article_id", "")))
            if aid:
                article_counts[aid] = article_counts.get(aid, 0) + 1
        elif et in ("report_download", "report_email_sent"):
            summary["report_downloads"] += 1
        elif et == "email_click":
            summary["email_clicks"] += 1
        elif et == "feedback_submit":
            summary["feedback_submits"] += 1

        if ind:
            summary["industry_breakdown"][ind] = (
                summary["industry_breakdown"].get(ind, 0) + 1
            )

    # Top articles by click count
    summary["top_articles"] = sorted(
        [{"article_id": k, "clicks": v} for k, v in article_counts.items()],
        key=lambda x: -x["clicks"],
    )[:5]

    return summary


def get_weekly_trend(days: int = 7) -> list[dict]:
    """최근 N일간 일별 요약 목록을 반환한다 (oldest first)."""
    today = datetime.now()
    trend = []
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append(get_daily_summary(d))
    return list(reversed(trend))
