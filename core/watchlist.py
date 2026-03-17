"""
core/watchlist.py
워치리스트 — 거시지표 임계값 모니터링 + 알림

data/watchlist.json 에 등록된 조건을 macro_data와 대조하여
임계값 초과 시 알림을 발송한다.
"""

import json
import pathlib
import smtplib
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

_ROOT = pathlib.Path(__file__).parent.parent
_WL_PATH = _ROOT / "data" / "watchlist.json"

# 지표명 → macro.json 키 매핑 (ID/한글 모두 지원)
_INDICATOR_ALIAS: dict[str, str] = {
    "USD_KRW": "환율(원/$)",
    "usd_krw": "환율(원/$)",
    "CPI": "소비자물가(CPI)",
    "cpi": "소비자물가(CPI)",
    "EXPORT_GROWTH": "수출증가율",
    "export_growth": "수출증가율",
    "BASE_RATE": "기준금리",
    "base_rate": "기준금리",
    "JPY_KRW": "원/100엔 환율",
    "jpy_krw": "원/100엔 환율",
    "EXPORT_PRICE_IDX": "수출물가지수",
    "export_price_idx": "수출물가지수",
    "IMPORT_PRICE_IDX": "수입물가지수",
    "import_price_idx": "수입물가지수",
}

# condition 한글 레이블
_COND_LABEL = {
    "above": "이상",
    "below": "이하",
    "change_pct": "변동률 초과",
}


# ─────────────────────────────────────────────────────────────
# 1. watchlist.json CRUD
# ─────────────────────────────────────────────────────────────

def _load_wl() -> dict:
    if _WL_PATH.exists():
        try:
            return json.loads(_WL_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"_meta": {}, "items": []}


def _save_wl(wl: dict) -> None:
    wl.setdefault("_meta", {})["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _WL_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WL_PATH.write_text(
        json.dumps(wl, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_items() -> list[dict]:
    """워치리스트 항목 전체를 반환한다."""
    return _load_wl().get("items", [])


# V9: 산업별 기본 워치리스트 항목
_INDUSTRY_DEFAULT_WATCHLIST: dict[str, list[dict]] = {
    "반도체": [
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": True},
        {"indicator": "수출증가율", "condition": "below", "threshold": -5, "notify_email": True},
        {"indicator": "수입물가지수", "condition": "above", "threshold": 5, "notify_email": False},
    ],
    "자동차": [
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "below", "threshold": 1250, "notify_email": True},
        {"indicator": "수입물가지수", "condition": "above", "threshold": 5, "notify_email": False},
    ],
    "화학": [
        {"indicator": "수입물가지수", "condition": "above", "threshold": 5, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": False},
        {"indicator": "소비자물가(CPI)", "condition": "above", "threshold": 3.5, "notify_email": False},
    ],
    "소비재": [
        {"indicator": "소비자물가(CPI)", "condition": "above", "threshold": 3.0, "notify_email": True},
        {"indicator": "기준금리", "condition": "above", "threshold": 3.75, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": False},
    ],
    "배터리": [
        {"indicator": "수출증가율", "condition": "below", "threshold": -5, "notify_email": True},
        {"indicator": "수입물가지수", "condition": "above", "threshold": 5, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": False},
    ],
    "조선": [
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "below", "threshold": 1250, "notify_email": True},
        {"indicator": "기준금리", "condition": "above", "threshold": 3.75, "notify_email": False},
    ],
    "철강": [
        {"indicator": "수입물가지수", "condition": "above", "threshold": 5, "notify_email": True},
        {"indicator": "수출증가율", "condition": "below", "threshold": -5, "notify_email": True},
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": False},
    ],
    "일반": [
        {"indicator": "환율(원/$)", "condition": "above", "threshold": 1450, "notify_email": True},
        {"indicator": "수출증가율", "condition": "below", "threshold": -5, "notify_email": True},
        {"indicator": "소비자물가(CPI)", "condition": "above", "threshold": 3.5, "notify_email": False},
    ],
}


def initialize_default_watchlist(industry_key: str) -> int:
    """산업별 기본 워치리스트를 초기화한다. 이미 항목이 있으면 건너뜀.

    Returns: 추가된 항목 수
    """
    wl = _load_wl()
    if wl.get("items"):
        return 0  # 이미 항목 존재

    defaults = _INDUSTRY_DEFAULT_WATCHLIST.get(industry_key, _INDUSTRY_DEFAULT_WATCHLIST["일반"])
    added = 0
    for d in defaults:
        add_item(
            indicator=d["indicator"],
            condition=d["condition"],
            threshold=d["threshold"],
            industry_keys=[industry_key, "일반"],
            notify_email=d["notify_email"],
        )
        added += 1
    return added


def add_item(
    indicator: str,
    condition: str,
    threshold: float,
    industry_keys: Optional[list[str]] = None,
    notify_email: bool = True,
) -> dict:
    """워치리스트 항목을 추가한다."""
    wl = _load_wl()
    items = wl.get("items", [])

    # ID 생성: indicator_condition_threshold
    safe_ind = indicator.replace("(", "").replace(")", "").replace("/", "_").replace(" ", "_")
    item_id = f"{safe_ind}_{condition}_{threshold}".lower()

    # 중복 체크
    for it in items:
        if it.get("id") == item_id:
            return it

    new_item = {
        "id": item_id,
        "indicator": indicator,
        "condition": condition,
        "threshold": threshold,
        "industry_keys": industry_keys or [],
        "notify_email": notify_email,
        "last_triggered": None,
    }
    items.append(new_item)
    wl["items"] = items
    _save_wl(wl)
    return new_item


def remove_item(item_id: str) -> bool:
    """워치리스트 항목을 삭제한다."""
    wl = _load_wl()
    items = wl.get("items", [])
    before = len(items)
    wl["items"] = [it for it in items if it.get("id") != item_id]
    if len(wl["items"]) < before:
        _save_wl(wl)
        return True
    return False


# ─────────────────────────────────────────────────────────────
# 2. 임계값 체크
# ─────────────────────────────────────────────────────────────

def _resolve_indicator(indicator: str) -> str:
    """indicator 키를 macro.json 키로 변환한다."""
    return _INDICATOR_ALIAS.get(indicator, indicator)


def _parse_value(raw) -> Optional[float]:
    """macro_data의 value를 float로 변환한다."""
    try:
        return float(str(raw).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


def check_watchlist(
    macro_data: dict,
    alert_channels_config: dict = None,
) -> list[dict]:
    """
    워치리스트의 모든 항목을 macro_data와 대조하여
    임계값을 초과한 항목 목록을 반환한다.

    중복 알림 방지: last_triggered가 24시간 이내이면 건너뜀.

    alert_channels_config가 제공되면 route_alert()를 호출하여
    Slack·카카오 등 다중 채널로 알림을 발송한다.

    Args:
        macro_data: {label: {value, unit, trend, prev_value, ...}} 형식
        alert_channels_config: 다중 채널 설정 (None이면 이메일만 기존 방식)

    Returns:
        [{"id", "indicator", "condition", "threshold", "current_value",
          "unit", "trend", "industry_keys", "notify_email"}, ...]
    """
    wl = _load_wl()
    items = wl.get("items", [])
    now = datetime.now()
    triggered = []
    updated = False

    for item in items:
        # 24시간 이내 재발송 방지
        last = item.get("last_triggered")
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if now - last_dt < timedelta(hours=24):
                    continue
            except (ValueError, TypeError):
                pass

        indicator_key = _resolve_indicator(item.get("indicator", ""))
        data = macro_data.get(indicator_key, {})
        if not isinstance(data, dict):
            continue

        current = _parse_value(data.get("value"))
        if current is None:
            continue

        condition = item.get("condition", "above")
        threshold = item.get("threshold", 0)
        hit = False

        if condition == "above" and current >= threshold:
            hit = True
        elif condition == "below" and current <= threshold:
            hit = True
        elif condition == "change_pct":
            prev = _parse_value(data.get("prev_value"))
            if prev is not None:
                if prev != 0:
                    change_pct = abs((current - prev) / prev * 100)
                    if change_pct >= threshold:
                        hit = True
                elif current != 0:
                    # 기저값 0 → 0이 아닌 값으로 변동 시 100% 변화 간주
                    hit = True

        if hit:
            item["last_triggered"] = now.strftime("%Y-%m-%d %H:%M:%S")
            updated = True
            triggered.append({
                "id": item["id"],
                "indicator": indicator_key,
                "condition": condition,
                "threshold": threshold,
                "current_value": current,
                "unit": data.get("unit", ""),
                "trend": data.get("trend", ""),
                "as_of": data.get("as_of", ""),
                "industry_keys": item.get("industry_keys", []),
                "notify_email": item.get("notify_email", False),
            })

    if updated:
        _save_wl(wl)

    # 다중 채널 알림 라우팅 (alert_channels_config 제공 시)
    if triggered and alert_channels_config is not None:
        try:
            from core.alert_channels import route_alert
            route_alert(triggered, alert_channels_config)
        except Exception as _route_err:
            print(f"[watchlist] 다중 채널 라우팅 오류: {_route_err}")

    return triggered


# ─────────────────────────────────────────────────────────────
# 3. 워치리스트 알림 이메일
# ─────────────────────────────────────────────────────────────

def _build_watchlist_alert_html(items: list[dict]) -> str:
    """워치리스트 알림 HTML을 생성한다."""
    cards = ""
    for it in items:
        cond_label = _COND_LABEL.get(it["condition"], it["condition"])
        industries = ", ".join(it.get("industry_keys", [])) or "전체"
        cards += f"""
        <div style="background:#fff3e0;border:2px solid #f97316;border-radius:8px;
                    padding:16px 20px;margin:10px 0">
          <div style="font-size:13px;font-weight:700;color:#ea580c;margin-bottom:8px">
            ⚠️ {it['indicator']} {cond_label} {it['threshold']}{it['unit']}
          </div>
          <div style="font-size:22px;font-weight:900;color:#1a202c">
            현재값: {it['current_value']}{it['unit']}
            <span style="font-size:14px;color:#ea580c">{it['trend']}</span>
          </div>
          <div style="font-size:11px;color:#666;margin-top:6px">
            기준일: {it.get('as_of', '')} · 관련 산업: {industries}
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fffbeb;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
<div style="max-width:580px;margin:28px auto;background:#fff;border-radius:12px;
            box-shadow:0 2px 16px rgba(249,115,22,.15)">
  <div style="background:#7c2d12;padding:24px 28px">
    <div style="font-size:11px;color:#fed7aa;letter-spacing:1.5px;margin-bottom:8px">
      WATCHLIST ALERT
    </div>
    <div style="font-size:22px;font-weight:900;color:#fff">
      ⚠️ 워치리스트 알림 ({len(items)}건)
    </div>
    <div style="font-size:12px;color:#fed7aa;margin-top:6px">
      {datetime.now().strftime("%Y-%m-%d %H:%M")} KST
    </div>
  </div>
  <div style="padding:20px 28px">{cards}</div>
  <div style="background:#fffbeb;padding:14px 28px;border-top:2px solid #fed7aa;
              font-size:11px;color:#92400e">
    본 알림은 워치리스트 설정에 따라 자동 발송됩니다 · 24시간 이내 동일 알림 재발송 없음
  </div>
</div>
</body>
</html>"""


def send_watchlist_alert(
    triggered_items: list[dict],
    to_email: Optional[str] = None,
) -> bool:
    """
    워치리스트 알림 이메일을 발송한다.

    Args:
        triggered_items: check_watchlist() 반환값 중 notify_email=True 항목
        to_email: 수신자 (None이면 EMAIL_RECIPIENTS 환경변수 사용)

    Returns:
        True — 발송 성공, False — 실패
    """
    email_items = [it for it in triggered_items if it.get("notify_email", False)]
    if not email_items:
        return False

    # emailer의 설정 로드 재사용
    from core.emailer import _load_config, is_configured

    if not is_configured():
        print("[watchlist] 이메일 설정 없음 — 알림 건너뜀")
        return False

    cfg = _load_config()
    recipients = [to_email] if to_email else [
        r.strip() for r in cfg["recipients"].split(",") if r.strip()
    ]
    if not recipients:
        return False

    # 제목 생성
    first = email_items[0]
    cond_label = _COND_LABEL.get(first["condition"], first["condition"])
    if len(email_items) == 1:
        subject = f"⚠️ [워치리스트] {first['indicator']} {cond_label} {first['threshold']}{first['unit']}"
    else:
        indicators = " · ".join(it["indicator"] for it in email_items[:3])
        subject = f"⚠️ [워치리스트] {indicators} 외 {len(email_items)}건 알림"

    html_body = _build_watchlist_alert_html(email_items)
    plain_body = "\n".join(
        f"[{it['indicator']}] 현재 {it['current_value']}{it['unit']} "
        f"({_COND_LABEL.get(it['condition'], '')} {it['threshold']}{it['unit']})"
        for it in email_items
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("60초 경제신호", cfg["sender"]))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        print(f"[watchlist] SMTP 연결 중: {cfg['smtp_host']}:{cfg['smtp_port']}")
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_bytes())
        print(f"[watchlist] ✓ 알림 발송 완료 → {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[watchlist] ✗ 발송 실패: {e}")
        traceback.print_exc()
        return False
