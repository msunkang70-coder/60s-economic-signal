"""
core/feedback_store.py
Fake Door 피드백 저장

저장 우선순위:
  1) Google Sheets  — Streamlit Secrets에 [gcp_service_account] + [gsheets] 설정 시
  2) 로컬 JSON      — data/feedback.json (폴백 / 로컬 개발용)

Google Sheets 행 구조:
  timestamp | industry | would_use | free_text
"""

import json
import os
import pathlib
import tempfile
from collections import Counter
from datetime import datetime

_ROOT = pathlib.Path(__file__).parent.parent
FEEDBACK_PATH = _ROOT / "data" / "feedback.json"

# ──────────────────────────────────────────────────────────────
# 1. 로컬 JSON 저장 (폴백)
# ──────────────────────────────────────────────────────────────

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


def _append_local(record: dict) -> None:
    """로컬 feedback.json에 레코드 추가."""
    records = _load_raw()
    records.append(record)
    _write_atomic(records)


# ──────────────────────────────────────────────────────────────
# 2. Google Sheets 연결
# ──────────────────────────────────────────────────────────────

_SHEET_HEADERS = ["timestamp", "industry", "would_use", "free_text"]


def _get_sheet():
    """
    Google Sheets worksheet 반환.
    Streamlit Secrets에 아래 두 섹션이 없으면 None 반환.

    [gcp_service_account]   — Service Account JSON 키 내용
    [gsheets]
    spreadsheet_id = "..."  — 시트 URL의 /d/XXXX/edit 부분
    """
    try:
        import streamlit as st
        import gspread
        from google.oauth2.service_account import Credentials

        creds_info = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)

        spreadsheet_id = st.secrets["gsheets"]["spreadsheet_id"]
        sheet = client.open_by_key(spreadsheet_id).sheet1

        # 헤더 행이 없으면 자동 추가
        existing = sheet.get_all_values()
        if not existing or existing[0] != _SHEET_HEADERS:
            sheet.insert_row(_SHEET_HEADERS, index=1)

        return sheet

    except Exception as e:
        print(f"[feedback] Google Sheets 연결 불가: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# 3. 공개 API
# ──────────────────────────────────────────────────────────────

def save_feedback(industry: str, would_use: str, free_text: str) -> dict:
    """
    피드백 1건 저장.
    - Google Sheets 가 연결돼 있으면 Sheets 에 append (+ 로컬 백업)
    - 연결 안 됐으면 로컬 JSON 에만 저장
    """
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industry":  industry,
        "would_use": would_use,
        "free_text":  free_text.strip(),
    }

    # 1) Google Sheets 시도
    sheet = _get_sheet()
    if sheet is not None:
        try:
            sheet.append_row([
                record["timestamp"],
                record["industry"],
                record["would_use"],
                record["free_text"],
            ])
            print(f"[feedback] Sheets 저장 완료: {record['industry']} / {record['would_use']}")
        except Exception as e:
            print(f"[feedback] Sheets append 실패: {e}")

    # 2) 로컬 JSON 에도 항상 백업
    try:
        _append_local(record)
    except Exception as e:
        print(f"[feedback] 로컬 저장 실패: {e}")

    return record


def load_feedback_summary() -> dict:
    """
    산업별 응답 집계 반환 (로컬 JSON 기반).

    Returns:
        {
            "total": int,
            "by_industry": {
                "반도체": {"count": 3, "would_use": {"예": 2, "아니오": 1}},
                ...
            },
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

    for v in by_industry.values():
        v["would_use"] = dict(v["would_use"])

    return {"total": len(records), "by_industry": by_industry}
