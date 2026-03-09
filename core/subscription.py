"""
core/subscription.py
B2B 구독 관리 모듈 — 구독자 CRUD + 산업별 발송 대상 조회

구독자 DB: data/subscribers.json
"""

import json
import pathlib
from datetime import datetime
from typing import Optional

_DB_PATH = pathlib.Path(__file__).parent.parent / "data" / "subscribers.json"


def _load_db() -> dict:
    """구독자 DB를 로드한다."""
    if _DB_PATH.exists():
        try:
            return json.loads(_DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"_meta": {}, "subscribers": [], "plans": {}}


def _save_db(db: dict) -> None:
    """구독자 DB를 저장한다."""
    db.setdefault("_meta", {})["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_subscribers(industry: Optional[str] = None, active_only: bool = True) -> list[dict]:
    """
    구독자 목록을 반환한다.

    Args:
        industry: 특정 산업 필터 (None이면 전체)
        active_only: True이면 active 구독자만

    Returns:
        [{"id", "company", "email", "industry", "plan", ...}, ...]
    """
    db = _load_db()
    subs = db.get("subscribers", [])

    if active_only:
        subs = [s for s in subs if s.get("active", False)]

    if industry:
        subs = [s for s in subs if s.get("industry") == industry]

    return subs


def get_subscriber_emails(industry: str) -> list[str]:
    """특정 산업의 활성 구독자 이메일 목록을 반환한다."""
    return [s["email"] for s in get_subscribers(industry=industry) if s.get("email")]


def add_subscriber(
    company: str,
    email: str,
    industry: str,
    plan: str = "free",
) -> dict:
    """
    새 구독자를 추가한다.

    Returns:
        추가된 구독자 dict
    """
    db = _load_db()
    subs = db.get("subscribers", [])

    # 중복 체크 (같은 이메일 + 산업)
    for s in subs:
        if s.get("email") == email and s.get("industry") == industry:
            # 이미 존재하면 활성화만
            s["active"] = True
            _save_db(db)
            return s

    # 새 ID 생성
    max_num = 0
    for s in subs:
        sid = s.get("id", "sub_000")
        try:
            num = int(sid.split("_")[1])
            max_num = max(max_num, num)
        except (IndexError, ValueError):
            pass

    new_sub = {
        "id": f"sub_{max_num + 1:03d}",
        "company": company,
        "email": email,
        "industry": industry,
        "plan": plan,
        "active": True,
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "preferences": {
            "frequency": "monthly",
            "include_macro": True,
            "include_script": True,
            "include_alert": True,
        },
    }

    subs.append(new_sub)
    db["subscribers"] = subs
    _save_db(db)
    return new_sub


def deactivate_subscriber(subscriber_id: str) -> bool:
    """구독자를 비활성화한다."""
    db = _load_db()
    for s in db.get("subscribers", []):
        if s.get("id") == subscriber_id:
            s["active"] = False
            _save_db(db)
            return True
    return False


def get_plans() -> dict:
    """요금제 정보를 반환한다."""
    db = _load_db()
    return db.get("plans", {})


def get_industry_send_list() -> dict[str, list[str]]:
    """
    산업별 발송 대상 이메일 목록을 반환한다.
    GitHub Actions에서 산업별 순차 발송에 사용.

    Returns:
        {"반도체": ["a@x.com", "b@y.com"], "자동차": ["c@z.com"], ...}
    """
    db = _load_db()
    result: dict[str, list[str]] = {}
    for s in db.get("subscribers", []):
        if not s.get("active", False):
            continue
        ind = s.get("industry", "일반")
        email = s.get("email", "")
        if email:
            result.setdefault(ind, []).append(email)
    return result
