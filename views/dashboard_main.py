"""views/dashboard_main.py — 메인 대시보드 레이아웃."""
import base64
import json
import os
import pathlib
from datetime import datetime as _dt

import streamlit as st

from core.industry_config import get_profile

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MACRO_PATH = pathlib.Path(_BASE) / "data" / "macro.json"


# ── 헤더 헬퍼 ────────────────────────────────────────────────────────

def _load_logo_b64() -> str:
    """assets/ 에서 로고 이미지를 base64로 로드. PNG 우선, 없으면 SVG 사용."""
    assets = pathlib.Path(_BASE) / "assets"
    for fname in ("logo.png", "logo.jpg", "logo.jpeg", "logo.svg"):
        p = assets / fname
        if p.exists():
            mime = "image/png" if fname.endswith(".png") else \
                   "image/jpeg" if fname.endswith((".jpg", ".jpeg")) else \
                   "image/svg+xml"
            b64 = base64.b64encode(p.read_bytes()).decode()
            return f"data:{mime};base64,{b64}"
    return ""


def _llm_badge_html() -> str:
    """헤더에 표시할 LLM 상태 배지 HTML 반환."""
    try:
        from core.summarizer import _get_llm_key
        has_llm = bool(_get_llm_key())
    except Exception:
        has_llm = False

    if has_llm:
        return (
            '<span style="background:rgba(251,191,36,0.15);color:#fbbf24;'
            'padding:2px 10px;border-radius:20px;font-size:10px;font-weight:700;'
            'border:1px solid rgba(251,191,36,0.3)">✦ Groq AI</span>'
        )
    return (
        '<span style="background:rgba(148,163,184,0.1);color:#64748b;'
        'padding:2px 10px;border-radius:20px;font-size:10px;font-weight:600;'
        'border:1px solid rgba(148,163,184,0.2)">규칙 기반</span>'
    )


def _freshness_badge_html() -> str:
    """macro.json 신선도에 따른 배지 HTML 반환."""
    try:
        from core.data_freshness import get_overall_freshness, render_freshness_badge
        if _MACRO_PATH.exists():
            raw = json.load(open(_MACRO_PATH, encoding="utf-8"))
            status = get_overall_freshness(raw)
            return render_freshness_badge(status)
    except Exception:
        pass
    return (
        '<span style="background:rgba(200,245,208,0.2);color:#C8F5D0;padding:2px 10px;'
        'border-radius:20px;font-size:10px;font-weight:700;'
        'border:1px solid rgba(200,245,208,0.4)">● LIVE</span>'
    )


# ── 메인 함수들 ──────────────────────────────────────────────────────

def render_dashboard_header() -> None:
    """MSion 브랜드 로고 + 다크 그라디언트 히어로 헤더."""
    # ── 업데이트 시각 ─────────────────────────────────
    refreshed_at = ""
    try:
        if _MACRO_PATH.exists():
            raw = json.load(open(_MACRO_PATH, encoding="utf-8"))
            rt = raw.get("_meta", {}).get("refreshed_at", "")
            if rt:
                dt = _dt.fromisoformat(rt)
                refreshed_at = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass

    # ── 로고 HTML (70px width) ────────────────────────
    logo_src = _load_logo_b64()
    if logo_src:
        logo_html = (
            f'<img src="{logo_src}" alt="MSion" '
            f'style="width:70px;height:auto;object-fit:contain;display:block;'
            f'filter:drop-shadow(0 2px 12px rgba(200,245,208,0.5))">'
        )
    else:
        # 폴백: 텍스트 로고 (큰 사이즈)
        logo_html = (
            '<div style="width:70px;height:70px;background:rgba(255,255,255,0.15);'
            'border-radius:16px;border:2px solid rgba(255,255,255,0.3);'
            'display:flex;align-items:center;justify-content:center;'
            'font-size:26px;font-weight:900;color:#ffffff;letter-spacing:-1px">'
            'M<span style="color:#C8F5D0">S</span>'
            '</div>'
        )

    # ── 태그 칩 ───────────────────────────────────────
    tags_html = "".join(
        f'<span style="background:rgba(200,245,208,0.2);color:#C8F5D0;'
        f'padding:3px 12px;border-radius:20px;font-size:11px;font-weight:600;'
        f'border:1px solid rgba(200,245,208,0.4);margin-right:6px">{t}</span>'
        for t in ["환율", "물가", "수출", "금리", "무역"]
    )

    st.html(f"""
    <div style="
        background:linear-gradient(135deg,#5B5FEE 0%,#3D40C4 50%,#5B5FEE 100%);
        border-radius:16px;padding:28px 36px 22px;margin-bottom:20px;
        border:1px solid rgba(255,255,255,0.2);
        box-shadow:0 4px 24px rgba(91,95,238,0.35);
    ">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">

        <!-- 좌: [로고] + [제목] 수평 배치 -->
        <div style="display:flex;align-items:center;gap:20px">

          <!-- 로고 (70px) -->
          <div style="flex-shrink:0">
            {logo_html}
          </div>

          <!-- 제목 + 부제목 -->
          <div>
            <div style="color:#C8F5D0;font-size:9px;font-weight:700;
                        letter-spacing:3px;text-transform:uppercase;margin-bottom:5px">
              MACRO SIGNAL INTELLIGENCE
            </div>
            <h1 style="color:#B8FAC8;font-size:30px;font-weight:900;margin:0 0 4px;
                       letter-spacing:-0.5px;line-height:1.15">
              60s 수출경제신호
            </h1>
            <p style="color:rgba(255,255,255,0.8);font-size:13px;margin:0;font-weight:500">
              AI Macro Intelligence Dashboard
            </p>
          </div>
        </div>

        <!-- 우: 업데이트 시각 + 배지들 -->
        <div style="text-align:right;flex-shrink:0;padding-top:4px">
          <div style="color:rgba(255,255,255,0.5);font-size:9px;font-weight:700;
                      text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">
            LAST UPDATED
          </div>
          <div style="color:#ffffff;font-size:16px;font-weight:800;
                      font-variant-numeric:tabular-nums;letter-spacing:-0.5px">
            {refreshed_at if refreshed_at else "—"}
          </div>
          <div style="color:rgba(255,255,255,0.5);font-size:10px;margin-top:3px">
            KST · 한국은행 ECOS
          </div>
          <div style="margin-top:12px;display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap">
            {_freshness_badge_html()}
            <span style="background:rgba(255,255,255,0.15);color:#ffffff;padding:2px 10px;
                         border-radius:20px;font-size:10px;font-weight:600;
                         border:1px solid rgba(255,255,255,0.25)">ECOS API</span>
            {_llm_badge_html()}
          </div>
        </div>

      </div>
      <!-- 하단 태그 칩 바 -->
      <div style="margin-top:18px;border-top:1px solid rgba(255,255,255,0.15);
                  padding-top:14px">{tags_html}</div>
    </div>
    """)


def render_executive_summary_slot(
    signal: dict | None = None,
    macro_data: dict | None = None,
) -> None:
    """Hero 헤더 아래 Executive Summary 슬롯.

    render_dashboard_header() 호출 직후에 배치한다.
    signal 이나 macro_data 중 하나라도 있으면 렌더링.
    """
    from views.executive_summary import render_executive_summary
    render_executive_summary(signal=signal, macro_data=macro_data)


def render_impact_strip(macro_data: dict) -> None:
    """Hero Card 아래, KPI 위에 3개 지표 신호등 스트립 렌더링."""
    if not macro_data:
        return

    # 임포트 지연 — app.py 내부 헬퍼를 직접 참조하지 않기 위해 로컬 정의
    _IMPACT_STRIP_INDICATORS = [
        ("환율(원/$)", "환율(원/$)"),
        ("소비자물가(CPI)", "CPI"),
        ("기준금리", "금리"),
    ]
    _STRIP_STATUS_MAP = {
        "danger":  ("🔴", "위험", "#ef4444"),
        "warning": ("🟠", "경고", "#f97316"),
        "caution": ("🟡", "주의", "#f59e0b"),
        "normal":  ("🟢", "정상", "#22c55e"),
    }
    _STRIP_TOOLTIP = {
        "환율(원/$)": "원/달러 환율이 높으면 수출 채산성은 개선되지만 수입 원가가 상승합니다",
        "소비자물가(CPI)": "CPI가 높으면 원가 상승·소비 위축 우려, 낮으면 비용 부담 완화",
        "기준금리": "고금리는 차입 비용 부담, 저금리는 투자·확장 자금 조달에 유리",
    }

    from core.macro_utils import _get_threshold_status

    cells = []
    for macro_key, display_name in _IMPACT_STRIP_INDICATORS:
        data = macro_data.get(macro_key)
        if not data:
            continue
        val_str = str(data.get("value", ""))
        status, _, _ = _get_threshold_status(macro_key, val_str)
        emoji, status_label, color = _STRIP_STATUS_MAP.get(status, ("🟢", "정상", "#22c55e"))
        tooltip = _STRIP_TOOLTIP.get(macro_key, "")
        cells.append((display_name, emoji, status_label, color, tooltip))

    if not cells:
        return

    cells_html = ""
    for idx, (display_name, emoji, status_label, color, tooltip) in enumerate(cells):
        border = "border-right:1px solid #e2e8f0;" if idx < len(cells) - 1 else ""
        cells_html += f"""
        <div style="flex:1;text-align:center;padding:10px 8px;cursor:help;
                    {border}" title="{tooltip}">
          <span style="font-size:13px;font-weight:700;color:#334155">{display_name}</span>
          <span style="margin-left:6px;font-size:14px">{emoji}</span>
          <span style="font-size:12px;font-weight:600;color:{color};margin-left:4px">{status_label}</span>
        </div>"""

    st.html(f"""
    <div style="display:flex;align-items:center;
                background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;
                margin-bottom:20px;overflow:hidden;font-family:'Inter',sans-serif">
      {cells_html}
    </div>
    """)


def render_industry_variable_card(industry_key: str, docs: list, macro_data: dict) -> None:
    """Tab 1 상단에 산업별 핵심 변수 카드 표시."""
    if industry_key == "일반":
        return

    from core.macro_utils import _get_threshold_status

    # ★ FIX: docs가 비어있으면 session_state에서 가져옴
    # (render_industry_variable_card가 기사 fetch 전에 호출될 수 있음)
    if not docs:
        docs = st.session_state.get("docs", [])

    profile = get_profile(industry_key)
    cv_list = profile["critical_variables"]

    # ── 핵심 변수 동의어 매핑 (기사 제목 매칭률 향상) ──
    _CV_SYNONYMS = {
        "글로벌 소비 경기": ["소비", "내수", "가계", "소비자", "소비재", "소매", "유통", "식품"],
        "물류비(해운운임)": ["물류", "해운", "운임", "운송", "컨테이너", "해상", "배송"],
        "소비자물가": ["물가", "CPI", "인플레", "소비자물가"],
        "미국 반도체 규제": ["반도체", "CHIPS", "수출통제", "수출 규제", "제재", "반도체 규제"],
        "AI 반도체 수요": ["AI", "GPU", "HBM", "AI반도체", "인공지능", "엔비디아"],
        "중국 수출 통제": ["중국", "수출통제", "희토류", "수출 제한", "대중"],
        "국제유가": ["유가", "원유", "나프타", "석유", "에너지"],
        "원자재 가격": ["원자재", "원자재 가격", "소재", "부품", "공급망"],
        "중국 경기": ["중국", "중국 경기", "중국 경제", "대중국"],
        "탄소국경조정(CBAM)": ["탄소", "CBAM", "탄소국경", "탄소중립", "배출"],
        "리튬 가격": ["리튬", "양극재", "배터리", "2차전지", "LFP", "NCM"],
        "전기차 판매": ["전기차", "EV", "전기차 판매", "전기차 보조금"],
        "미국 IRA": ["IRA", "인플레이션감축법", "미국 보조금"],
        "선박 수주": ["선박", "조선", "수주", "LNG선", "선박 수주"],
        "해운 운임": ["해운", "운임", "BDI", "컨테이너", "해운 운임"],
        "철강 가격": ["철강", "열연", "냉연", "철광석", "철강 가격"],
        "철광석 가격": ["철광석", "철강", "원자재", "포스코"],
        "중국 철강 수출": ["중국 철강", "과잉 공급", "중국 수출"],
        "미국 관세 정책": ["관세", "통상", "무역 분쟁", "미국 관세"],
        "전기차 보조금": ["전기차", "보조금", "EV", "IRA"],
        "환율(원/$)": ["환율", "원달러", "달러", "원화"],
    }

    # ★ 확장 키워드도 동의어 매칭에 활용
    try:
        from ui.article_cards import _INDUSTRY_EXTENDED_KW
        _ext_kws = _INDUSTRY_EXTENDED_KW.get(industry_key, [])
    except ImportError:
        _ext_kws = []

    items_html = ""
    for cv in cv_list:
        # 거시지표와 매칭되는 변수는 현재값 표시
        macro_match = macro_data.get(cv)
        if macro_match:
            val = macro_match.get("value", "")
            trend = macro_match.get("trend", "")
            status, _, status_label = _get_threshold_status(cv, str(val))
            status_badge = f' <span style="color:#dc2626;font-size:11px">⚠️{status_label}</span>' if status in ("warning", "danger", "caution") else ""
            items_html += f'<div style="margin:4px 0;font-size:13px">📌 {cv} → {val} {trend}{status_badge}</div>'
        else:
            # 동의어 매핑 + 확장 키워드로 기사 매칭 수 카운트
            synonyms = _CV_SYNONYMS.get(cv, [cv.replace("(", "").replace(")", "")])
            # 확장 키워드에서 관련 있는 것도 추가
            all_match_kws = list(synonyms) + [kw for kw in _ext_kws if any(s in kw or kw in s for s in synonyms)]
            count = sum(
                1 for d in docs
                if any(syn in d.get("title", "") for syn in all_match_kws)
            )
            items_html += f'<div style="margin:4px 0;font-size:13px">📌 {cv} → 관련 기사 {count}건</div>'

    # ── KITA 수출 현황 추가 ──────────────────────────────────
    kita_html = ""
    try:
        from core.kita_source import fetch_kita_export_trend
        _kita = fetch_kita_export_trend(industry_key)
        if _kita.get("export_amount") or _kita.get("yoy_change"):
            _kita_parts = []
            if _kita["export_amount"]:
                _kita_parts.append(_kita["export_amount"])
            if _kita["yoy_change"]:
                _kita_parts.append(f"({_kita['yoy_change']} YoY)")
            _kita_text = " ".join(_kita_parts)
            _kita_period = f" [{_kita['period']}]" if _kita.get("period") else ""
            kita_html = (
                f'<div style="margin-top:8px;padding:8px 12px;background:#eff6ff;'
                f'border-radius:8px;font-size:12px;color:#1e40af">'
                f'📦 {_kita["industry"]} 수출: {_kita_text}{_kita_period}</div>'
            )
    except Exception as _kita_err:
        print(f"[app] KITA 수출 현황 로드 실패: {_kita_err}")

    st.html(f"""
    <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;
                padding:16px 20px;margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#16a34a;margin-bottom:8px">
        {profile['icon']} {profile['label']} 핵심 변수
      </div>
      {items_html}
      {kita_html}
    </div>
    """)


# ── 리스크 게이지 ─────────────────────────────────────────────

def render_risk_gauge(risk_data: dict) -> None:
    """리스크 지수 게이지 (0-100). HTML/CSS 프로그레스 바. risk_data None이면 패스."""
    if not risk_data:
        return

    score = risk_data.get("score", 0)
    label = risk_data.get("label", "")
    description = risk_data.get("description", "")

    # 점수 구간별 색상
    if score >= 75:
        bar_color = "#dc2626"
        status_text = "높음"
        bg_color = "#fef2f2"
        border_color = "#fca5a5"
    elif score >= 50:
        bar_color = "#f97316"
        status_text = "경고"
        bg_color = "#fff7ed"
        border_color = "#fed7aa"
    elif score >= 25:
        bar_color = "#f59e0b"
        status_text = "주의"
        bg_color = "#fefce8"
        border_color = "#fde68a"
    else:
        bar_color = "#22c55e"
        status_text = "안정"
        bg_color = "#f0fdf4"
        border_color = "#86efac"

    clamped = max(0, min(100, score))

    desc_html = (
        f'<div style="font-size:12px;color:#475569;margin-top:8px">{description}</div>'
        if description else ""
    )
    label_html = (
        f'<span style="font-size:12px;color:#64748b;margin-left:8px">({label})</span>'
        if label else ""
    )

    st.html(f"""
    <div style="background:{bg_color};border:1px solid {border_color};border-radius:12px;
                padding:16px 20px;margin-bottom:16px;font-family:'Inter',sans-serif;
                box-shadow:0 2px 8px rgba(0,0,0,0.04)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:13px;font-weight:700;color:#1e293b">
          🛡️ 복합 리스크 지수{label_html}
        </div>
        <div style="font-size:20px;font-weight:800;color:{bar_color}">
          {clamped}<span style="font-size:12px;color:#64748b">/100</span>
          <span style="font-size:11px;font-weight:600;margin-left:6px;
                       background:{bg_color};color:{bar_color};padding:2px 8px;
                       border-radius:8px;border:1px solid {border_color}">{status_text}</span>
        </div>
      </div>
      <div style="background:#e2e8f0;border-radius:6px;height:10px;overflow:hidden">
        <div style="width:{clamped}%;height:100%;background:{bar_color};
                    border-radius:6px;transition:width 0.5s ease"></div>
      </div>
      {desc_html}
    </div>
    """)


# ── 충격 알림 배너 ────────────────────────────────────────────

def render_shock_alert_banner(shocks: list) -> None:
    """충격 알림 배너. 빈 리스트면 패스. severity별 색상 구분."""
    if not shocks:
        return

    _SEV_STYLES = {
        "critical": {"bg": "#fef2f2", "border": "#dc2626", "icon": "🚨", "text": "#991b1b"},
        "high":     {"bg": "#fff7ed", "border": "#f97316", "icon": "⚠️", "text": "#9a3412"},
        "medium":   {"bg": "#fefce8", "border": "#f59e0b", "icon": "⚡", "text": "#92400e"},
        "low":      {"bg": "#eff6ff", "border": "#3b82f6", "icon": "ℹ️", "text": "#1e40af"},
    }

    items_html = ""
    for shock in shocks:
        severity = shock.get("severity", "medium")
        style = _SEV_STYLES.get(severity, _SEV_STYLES["medium"])
        title = shock.get("title", "충격 감지")
        desc = shock.get("description", "")
        indicator = shock.get("indicator", "")

        indicator_html = (
            f'<span style="background:rgba(0,0,0,0.06);padding:2px 8px;border-radius:6px;'
            f'font-size:11px;font-weight:600;margin-left:8px">{indicator}</span>'
            if indicator else ""
        )
        desc_html = (
            f'<div style="font-size:12px;color:{style["text"]};margin-top:4px;opacity:0.85">{desc}</div>'
            if desc else ""
        )

        items_html += f"""
        <div style="background:{style['bg']};border:1px solid {style['border']};
                    border-left:4px solid {style['border']};border-radius:10px;
                    padding:12px 16px;margin-bottom:8px;font-family:'Inter',sans-serif;
                    box-shadow:0 2px 8px rgba(0,0,0,0.04)">
          <div style="display:flex;align-items:center">
            <span style="font-size:16px;margin-right:8px">{style['icon']}</span>
            <span style="font-size:13px;font-weight:700;color:{style['text']}">{title}</span>
            {indicator_html}
          </div>
          {desc_html}
        </div>"""

    st.html(f"""
    <div style="margin-bottom:16px">
      {items_html}
    </div>
    """)


# ── 데이터 신선도 스트립 ──────────────────────────────────────

def render_data_quality_strip(macro_data: dict) -> None:
    """데이터 신선도 스트립. as_of 기준 배지 표시."""
    if not macro_data:
        return

    badges_html = ""
    for key, data in macro_data.items():
        if key.startswith("_"):
            continue
        if not isinstance(data, dict):
            continue

        as_of = data.get("as_of", "")
        label = key
        if not as_of:
            # 날짜 정보 없으면 회색 배지
            badges_html += (
                f'<span style="background:#f1f5f9;color:#64748b;padding:3px 10px;'
                f'border-radius:8px;font-size:11px;font-weight:600;'
                f'border:1px solid #e2e8f0;margin:3px">{label} —</span>'
            )
            continue

        # as_of 날짜 기준 신선도 판정
        try:
            from datetime import datetime as _dtm
            _as_of_dt = _dtm.strptime(as_of[:10], "%Y-%m-%d")
            _age_days = (_dtm.now() - _as_of_dt).days
        except Exception:
            _age_days = 999

        if _age_days <= 1:
            _badge_bg = "#dcfce7"
            _badge_color = "#166534"
            _badge_border = "#86efac"
            _freshness = "LIVE"
        elif _age_days <= 7:
            _badge_bg = "#fefce8"
            _badge_color = "#92400e"
            _badge_border = "#fde68a"
            _freshness = f"{_age_days}d"
        else:
            _badge_bg = "#fef2f2"
            _badge_color = "#991b1b"
            _badge_border = "#fecaca"
            _freshness = f"{_age_days}d"

        badges_html += (
            f'<span style="background:{_badge_bg};color:{_badge_color};padding:3px 10px;'
            f'border-radius:8px;font-size:11px;font-weight:600;'
            f'border:1px solid {_badge_border};margin:3px">'
            f'{label} {_freshness}</span>'
        )

    if not badges_html:
        return

    st.html(f"""
    <div style="background:white;border:1px solid #e2e8f0;border-radius:12px;
                padding:12px 16px;margin-bottom:16px;font-family:'Inter',sans-serif;
                box-shadow:0 2px 8px rgba(0,0,0,0.04)">
      <div style="font-size:11px;font-weight:700;color:#64748b;margin-bottom:8px;
                  text-transform:uppercase;letter-spacing:1px">DATA FRESHNESS</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px">
        {badges_html}
      </div>
    </div>
    """)
