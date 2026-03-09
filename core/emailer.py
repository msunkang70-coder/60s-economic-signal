"""
core/emailer.py
60초 경제신호 — 이메일 발송 모듈

설정 방법 (우선순위 순):
  1. 환경변수 (GitHub Actions Secret 또는 로컬 .env):
       EMAIL_SENDER        발신자 주소 (Gmail 권장)
       EMAIL_PASSWORD      앱 비밀번호 (Gmail: myaccount.google.com/apppasswords)
       EMAIL_RECIPIENTS    수신자 주소 — 쉼표 구분 (예: a@x.com,b@y.com)
       EMAIL_SMTP_HOST     SMTP 서버 (기본: smtp.gmail.com)
       EMAIL_SMTP_PORT     SMTP 포트 (기본: 587)

  2. Streamlit Secrets (.streamlit/secrets.toml):
       [email]
       sender     = "you@gmail.com"
       password   = "앱비밀번호16자리"
       recipients = "a@x.com,b@y.com"
       smtp_host  = "smtp.gmail.com"   # 선택 (기본값)
       smtp_port  = 587                # 선택 (기본값)

  설정 없으면 발송을 건너뜀 — Backward Compatible.

Gmail 앱 비밀번호 발급:
  Google 계정 → 보안 → 2단계 인증 활성화 → 앱 비밀번호 → 생성
  (일반 비밀번호 대신 16자리 앱 비밀번호를 EMAIL_PASSWORD에 입력)

CLI 테스트:
  python -m core.emailer
"""

import json
import os
import pathlib
import smtplib
import traceback
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

from core.industry_config import get_profile

_ROOT = pathlib.Path(__file__).parent.parent

# ─────────────────────────────────────────────────────────────
# T-09: 대시보드 URL 설정 (Streamlit Cloud 배포 시 변경)
# ─────────────────────────────────────────────────────────────
_DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8501").rstrip("/")

# UTM 파라미터
_UTM_PARAMS = "utm_source=email&utm_medium=newsletter&utm_campaign=monthly"


# ─────────────────────────────────────────────────────────────
# 0-1. 산업 설정 로드
# ─────────────────────────────────────────────────────────────

def _load_industry() -> tuple[str, dict]:
    """환경변수 INDUSTRY → Streamlit Secrets 순으로 산업 키를 결정한다.

    Returns:
        (industry_key, profile_dict)
    """
    key = os.environ.get("INDUSTRY", "").strip()
    if not key:
        try:
            import streamlit as st
            key = (st.secrets.get("email") or {}).get("industry", "").strip()
        except Exception:
            pass
    if not key:
        key = "일반"
    return key, get_profile(key)


# ─────────────────────────────────────────────────────────────
# 0. 기사 ↔ 거시지표 키워드 매핑
# ─────────────────────────────────────────────────────────────

_INDICATOR_KEYWORDS: dict[str, list[str]] = {
    "환율": ["환율", "달러", "원화", "외환"],
    "소비자물가(CPI)": ["물가", "CPI", "인플레", "소비자"],
    "기준금리": ["금리", "한국은행", "통화정책", "기준금리"],
    "수출증가율": ["수출", "무역", "교역", "수출입"],
    "코스피": ["코스피", "주가", "주식", "증시", "상장", "코스닥"],
    "수입물가지수": ["수입", "원자재", "원가"],
    "원/100엔 환율": ["엔화", "일본", "엔저", "엔고"],
    "GDP성장률": ["성장률", "GDP", "경제성장", "성장"],
}


def _match_indicators(title: str) -> list[str]:
    """기사 제목에서 관련 거시지표 이름 목록을 반환한다.

    Args:
        title: 기사 제목 문자열

    Returns:
        매칭된 지표 이름 리스트. 매칭 없으면 빈 리스트.
    """
    matched = []
    for indicator, keywords in _INDICATOR_KEYWORDS.items():
        for kw in keywords:
            if kw in title:
                matched.append(indicator)
                break  # 해당 지표는 한 번만 추가
    return matched



# ─────────────────────────────────────────────────────────────
# 1. 설정 로드
# ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """환경변수 → Streamlit Secrets 순으로 이메일 설정을 반환한다."""
    cfg: dict = {
        "sender":     os.environ.get("EMAIL_SENDER", "").strip(),
        "password":   os.environ.get("EMAIL_PASSWORD", "").strip(),
        "recipients": os.environ.get("EMAIL_RECIPIENTS", "").strip(),
        "smtp_host":  os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com").strip(),
        "smtp_port":  int(os.environ.get("EMAIL_SMTP_PORT", "587")),
    }

    # 환경변수에 필수값이 없으면 Streamlit Secrets 시도
    if not cfg["sender"] or not cfg["password"] or not cfg["recipients"]:
        try:
            import streamlit as st
            sec = st.secrets.get("email") or {}
            cfg["sender"]     = cfg["sender"]     or sec.get("sender", "").strip()
            cfg["password"]   = cfg["password"]   or sec.get("password", "").strip()
            cfg["recipients"] = cfg["recipients"] or sec.get("recipients", "").strip()
            cfg["smtp_host"]  = cfg["smtp_host"]  or sec.get("smtp_host", "smtp.gmail.com")
            cfg["smtp_port"]  = cfg["smtp_port"]  or int(sec.get("smtp_port", 587))
        except Exception:
            pass  # Streamlit 컨텍스트 아님 — 무시

    return cfg


def is_configured() -> bool:
    """이메일 발송에 필요한 최소 설정이 돼 있으면 True."""
    cfg = _load_config()
    return bool(cfg["sender"] and cfg["password"] and cfg["recipients"])


# ─────────────────────────────────────────────────────────────
# 2. 이메일 본문 생성
# ─────────────────────────────────────────────────────────────

def _load_macro() -> dict:
    """data/macro.json을 로드해 반환. 파일 없으면 빈 dict.

    '_'로 시작하는 메타 키(_meta 등)는 이메일 카드 렌더링에서 제외한다.
    """
    p = _ROOT / "data" / "macro.json"
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            # _meta 등 내부 키 제외
            return {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception:
            pass
    return {}


def _build_html(script_text: str, macro: dict, issue_month: str,
                 industry_label: str = "", industry_desc: str = "") -> str:
    """60초 스크립트 + 거시지표를 담은 HTML 이메일 본문을 생성한다."""

    # 거시지표 카드 HTML
    _now = datetime.now()

    def _as_of_style(as_of_str: str) -> tuple:
        """as_of 문자열을 파싱해 (색상, 추가레이블) 반환.

        기준: 현재 날짜보다 1개월 초과 과거 → 오래된 데이터(orange + 경고 레이블).
        1개월 이내 → 최신(green). 파싱 불가 → gray, 레이블 없음.
        """
        if not as_of_str:
            return "#aaa", ""
        try:
            # "YYYY-MM-DD" 또는 "YYYY-MM" 형식 모두 지원
            if len(as_of_str) == 7:
                as_of_dt = datetime.strptime(as_of_str, "%Y-%m")
            else:
                as_of_dt = datetime.strptime(as_of_str[:10], "%Y-%m-%d")
            # 월 단위 차이 계산
            months_diff = (_now.year - as_of_dt.year) * 12 + (_now.month - as_of_dt.month)
            if months_diff > 1:
                return "#f97316", " &nbsp;<span style=\"font-size:9px;color:#f97316\">⚠ 최근 미발표</span>"
            else:
                return "#2d9b4e", ""
        except (ValueError, TypeError):
            return "#aaa", ""

    macro_cards = ""
    for label, d in macro.items():
        trend = d.get("trend", "")
        trend_color = "#e53e3e" if trend == "▲" else ("#2d9b4e" if trend == "▼" else "#888")
        as_of_color, as_of_extra = _as_of_style(d.get("as_of", ""))
        macro_cards += f"""
        <div style="display:inline-block;min-width:160px;margin:6px;
                    padding:16px 20px;border:1px solid #e2e8f0;
                    border-radius:8px;vertical-align:top">
          <div style="font-size:11px;color:#888;margin-bottom:4px">{label}</div>
          <div style="font-size:22px;font-weight:800;color:#1a202c">
            {d.get("value","")}{d.get("unit","")}
            <span style="font-size:14px;color:{trend_color}">{trend}</span>
          </div>
          <div style="font-size:11px;color:#666;margin-top:4px">{d.get("note","")}</div>
          <div style="font-size:10px;color:{as_of_color};margin-top:4px">기준일: {d.get("as_of","")}{as_of_extra}</div>
        </div>"""

    # 이슈 번호별 배지 색상 (TASK-02)
    _ISSUE_COLORS = {
        "1": ("#1e40af", "#dbeafe"),
        "2": ("#065f46", "#d1fae5"),
        "3": ("#7c2d12", "#ffedd5"),
    }

    def _issue_badge(num: str, content: str) -> str:
        fg, bg = _ISSUE_COLORS.get(num, ("#374151", "#f3f4f6"))
        return (
            f'<div style="display:flex;align-items:flex-start;margin:5px 0;'
            f'padding:8px 12px;background:{bg};border-radius:6px">'
            f'<span style="min-width:44px;font-size:11px;font-weight:700;color:#fff;'
            f'background:{fg};border-radius:4px;padding:2px 7px;'
            f'margin-right:10px;flex-shrink:0;line-height:1.6">이슈 {num}</span>'
            f'<span style="font-size:13px;color:#1a202c;line-height:1.7">{content}</span>'
            f'</div>'
        )

    def _interp_header(num: str, content: str) -> str:
        fg, bg = _ISSUE_COLORS.get(num, ("#374151", "#f3f4f6"))
        return (
            f'<div style="margin:10px 0 3px;padding:5px 12px;'
            f'background:{fg};border-radius:4px 4px 0 0;display:inline-block">'
            f'<span style="font-size:11px;font-weight:700;color:#fff">'
            f'▶ 이슈 {num} 해석</span></div>'
            f'<div style="margin:0 0 8px;padding:8px 12px;'
            f'background:#f8fafc;border:1px solid #e2e8f0;border-radius:0 4px 4px 4px;'
            f'font-size:13px;color:#374151;line-height:1.7">{content}</div>'
        )

    import re as _re

    # 스크립트 본문을 HTML로 변환 (줄바꿈 → <br>)
    script_html = ""
    for line in script_text.splitlines():
        stripped = line.strip()
        if not stripped:
            script_html += "<br>"
        elif stripped.startswith("[") and stripped.endswith("]"):
            # 시간 태그 → 섹션 헤더
            script_html += (
                f'<div style="font-weight:700;color:#3a5fc8;'
                f'margin:18px 0 6px;font-size:13px">{stripped}</div>'
            )
        elif stripped.startswith("이슈") and ":" in stripped and not stripped.startswith("▶"):
            # 이슈 배지 카드 (이슈N: 형식)
            m = _re.match(r"이슈(\d+):\s*(.*)", stripped)
            if m:
                script_html += _issue_badge(m.group(1), m.group(2))
            else:
                script_html += f'<p style="margin:4px 0;line-height:1.8;color:#444">{stripped}</p>'
        elif stripped.startswith("▶이슈") and "해석:" in stripped:
            # 이슈별 해석 헤더 카드 (▶이슈N 해석: 형식)
            m = _re.match(r"▶이슈(\d+) 해석:\s*(.*)", stripped)
            if m:
                script_html += _interp_header(m.group(1), m.group(2))
            else:
                script_html += f'<p style="margin:4px 0;line-height:1.8;color:#444">{stripped}</p>'
        elif stripped.startswith("※"):
            # 참고 기사 이후 — 별도 처리
            script_html += f'<div style="color:#aaa;font-size:11px">{stripped}</div>'
        elif stripped.startswith("---"):
            script_html += '<hr style="border:none;border-top:1px solid #e2e8f0;margin:10px 0">'
        else:
            script_html += f'<p style="margin:4px 0;line-height:1.8;color:#444">{stripped}</p>'


    # ── TASK-04: 기사 ↔ 지표 연결 — ※ 참고 기사 목록 파싱 ───────────────
    import re as _re2
    _articles: list[tuple[str, str]] = []  # [(num, title), ...]
    _in_refs = False
    for _line in script_text.splitlines():
        _s = _line.strip()
        if _s.startswith("※ 참고 기사 목록"):
            _in_refs = True
            continue
        if _in_refs:
            _m = _re2.match(r"\[(\d+)\]\s*(.+)", _s)
            if _m:
                _articles.append((_m.group(1), _m.group(2).strip()))
            elif _s.startswith("http") or not _s:
                continue
            elif _s and not _s.startswith("["):
                break  # 다른 섹션 시작

    # 기사 카드 HTML 생성
    _BADGE_COLORS = [
        ("#1e40af", "#dbeafe"),
        ("#065f46", "#d1fae5"),
        ("#7c2d12", "#ffedd5"),
    ]
    _INDICATOR_PILL_COLOR = "#3a5fc8"
    article_link_cards = ""
    for _num, _title in _articles:
        _bi = int(_num) - 1
        _fg, _bg = _BADGE_COLORS[_bi] if _bi < len(_BADGE_COLORS) else ("#374151", "#f3f4f6")
        _short_title = _title
        _indicators = _match_indicators(_title)
        if _indicators:
            _pills = "".join(
                f'<span style="display:inline-block;font-size:10px;font-weight:600;'
                f'color:#fff;background:{_INDICATOR_PILL_COLOR};'
                f'border-radius:10px;padding:2px 8px;margin:2px 3px 2px 0;'
                f'white-space:nowrap">{_ind}</span>'
                for _ind in _indicators
            )
        else:
            _pills = (
                '<span style="display:inline-block;font-size:10px;font-weight:600;'
                'color:#888;background:#e5e7eb;'
                'border-radius:10px;padding:2px 8px;margin:2px 3px 2px 0">일반 경제동향</span>'
            )
        # T-09: 기사 제목을 대시보드 앵커 링크로 연결
        if _DASHBOARD_URL:
            _title_html = (
                f'<a href="{_DASHBOARD_URL}?article_id={_num}&{_UTM_PARAMS}" '
                f'style="color:#1a202c;text-decoration:none;font-weight:600">'
                f'{_short_title}</a>'
            )
        else:
            _title_html = _short_title
        article_link_cards += (
            f'<div style="display:flex;align-items:flex-start;margin:8px 0;'
            f'padding:10px 14px;background:#fafafa;border:1px solid #e2e8f0;border-radius:8px">'
            f'<span style="min-width:28px;height:28px;line-height:28px;text-align:center;'
            f'font-size:12px;font-weight:800;color:#fff;background:{_fg};'
            f'border-radius:50%;flex-shrink:0;margin-right:12px;margin-top:1px">{_num}</span>'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-size:13px;color:#1a202c;font-weight:600;'
            f'margin-bottom:5px;line-height:1.5">{_title_html}</div>'
            f'<div style="line-height:1.6">{_pills}</div>'
            f'</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
<div style="max-width:640px;margin:32px auto;background:#fff;border-radius:12px;
            box-shadow:0 2px 16px rgba(0,0,0,.08)">

  <!-- 헤더 -->
  <div style="background:#1a202c;padding:28px 32px">
    <div style="font-size:11px;color:#90cdf4;letter-spacing:1px;margin-bottom:6px">
      MONTHLY ECONOMIC SIGNAL
    </div>
    <div style="font-size:22px;font-weight:900;color:#fff">
      📊 {f"[{industry_label}] " if industry_label else ""}60초 경제신호 — {issue_month}
    </div>
    <div style="font-size:12px;color:#a0aec0;margin-top:6px">
      {f"{industry_label} 수출기업을 위한 60초 경제 브리핑" if industry_label else "매월 KDI 나라경제 이슈를 60초로 요약합니다"}
    </div>
  </div>

  <!-- 거시지표 -->
  <div style="padding:24px 32px;border-bottom:1px solid #e2e8f0">
    <div style="font-size:13px;font-weight:700;color:#1a202c;margin-bottom:12px">
      📈 이번 달 주요 거시지표
    </div>
    <div style="font-size:11px;color:#888;margin-bottom:8px">
      ※ 일부 지표는 통계 특성상 최근 2~3개월 데이터가 최신입니다.
    </div>
    <div>{macro_cards}</div>
  </div>

  <!-- 기사 ↔ 지표 연결 -->
  <div style="padding:20px 32px;border-bottom:1px solid #e2e8f0">
    <div style="font-size:13px;font-weight:700;color:#1a202c;margin-bottom:12px">
      🔗 기사 ↔ 지표 연결
    </div>
    <div>{article_link_cards}</div>
  </div>

  <!-- 60초 스크립트 -->
  <div style="padding:28px 32px">
    <div style="font-size:13px;font-weight:700;color:#1a202c;margin-bottom:16px">
      🎬 60초 쇼츠 스크립트
    </div>
    <div style="background:#f8fafc;border-left:4px solid #3a5fc8;
                padding:20px 24px;border-radius:0 8px 8px 0;font-size:13px">
      {script_html}
    </div>
  </div>

  <!-- T-09: 대시보드 링크 -->
  {"" if not _DASHBOARD_URL else f'''<div style="padding:20px 32px;text-align:center">
    <a href="{_DASHBOARD_URL}?{_UTM_PARAMS}"
       style="display:inline-block;padding:14px 32px;
              background:#3a5fc8;color:#fff;font-weight:700;
              font-size:14px;text-decoration:none;border-radius:8px">
      📊 대시보드에서 상세 보기
    </a>
  </div>'''}

  <!-- 푸터 -->
  <div style="background:#f8fafc;padding:18px 32px;
              border-top:1px solid #e2e8f0;font-size:11px;color:#a0aec0">
    본 메일은 GitHub Actions에 의해 자동 발송됩니다 &nbsp;|&nbsp;
    출처: KDI 경제정보센터 나라경제 &nbsp;|&nbsp;
    생성일: {datetime.now().strftime("%Y-%m-%d %H:%M")} KST
  </div>
</div>
</body>
</html>"""


def _build_plain(script_text: str, macro: dict, issue_month: str,
                  industry_label: str = "") -> str:
    """HTML을 지원하지 않는 클라이언트용 plaintext 본문."""
    _prefix = f"[{industry_label}] " if industry_label else ""
    lines = [
        f"[{_prefix}60초 경제신호 — {issue_month}]",
        "=" * 50,
        "",
        "▶ 이번 달 주요 거시지표",
        "-" * 30,
    ]
    for label, d in macro.items():
        lines.append(f"  {label}: {d.get('value','')}{d.get('unit','')} {d.get('trend','')}  ({d.get('as_of','')})")

    lines += ["", "▶ 60초 쇼츠 스크립트", "-" * 30, "", script_text, "",
              "─" * 50,
              "본 메일은 GitHub Actions에 의해 자동 발송됩니다.",
              "출처: KDI 경제정보센터 나라경제"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 3. 발송 (메인 함수)
# ─────────────────────────────────────────────────────────────

def send_script_email(
    script_path=None,
    srt_path: Optional[str] = None,
    attach_srt: bool = True,
) -> bool:
    """
    60초 스크립트 이메일을 발송한다.

    Args:
        script_path: output_script.txt 경로 (None이면 outputs/output_script.txt)
        srt_path:    output_script.srt 경로 (None이면 outputs/output_script.srt)
        attach_srt:  True이면 .srt 파일을 첨부 (기본값: True)

    Returns:
        True  — 발송 성공
        False — 설정 없음 또는 발송 실패 (예외 없이 반환)
    """
    if not is_configured():
        print("[emailer] 이메일 설정 없음 — 발송 건너뜀")
        print("  설정 방법: 환경변수 EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENTS")
        return False

    cfg = _load_config()

    # ── 파일 경로 결정 ──────────────────────────────────────
    script_p = pathlib.Path(script_path) if script_path else _ROOT / "outputs" / "output_script.txt"
    srt_p    = pathlib.Path(srt_path)    if srt_path    else _ROOT / "outputs" / "output_script.srt"

    if not script_p.exists():
        print(f"[emailer] 스크립트 파일 없음: {script_p}")
        return False

    script_text = script_p.read_text(encoding="utf-8")

    # 발행 월 추출 (스크립트 2번째 줄에서)
    issue_month = ""
    for line in script_text.splitlines():
        if "페이지 제목:" in line:
            # "페이지 제목: 나라경제 | KDI 경제교육·정보센터" 형식
            break
        if "생성일시:" in line:
            try:
                dt_str = line.split("생성일시:")[1].strip()[:7]  # "YYYY-MM"
                y, m = dt_str.split("-")
                issue_month = f"{y}년 {int(m):02d}월"
            except Exception:
                pass

    if not issue_month:
        issue_month = datetime.now().strftime("%Y년 %m월")

    macro = _load_macro()

    # ── 산업 설정 ─────────────────────────────────────────
    ind_key, ind_profile = _load_industry()
    ind_label = ind_profile.get("label", "")
    ind_desc  = ind_profile.get("description", "")

    # ── 이메일 조립 ─────────────────────────────────────────
    recipients = [r.strip() for r in cfg["recipients"].split(",") if r.strip()]
    _ind_tag   = f"[{ind_label}] " if ind_label and ind_key != "일반" else ""
    subject    = f"📊 {_ind_tag}60초 경제신호 — {issue_month}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("60초 경제신호", cfg["sender"]))
    msg["To"]      = ", ".join(recipients)

    plain_body = _build_plain(script_text, macro, issue_month,
                               industry_label=ind_label if ind_key != "일반" else "")
    html_body  = _build_html(script_text, macro, issue_month,
                              industry_label=ind_label if ind_key != "일반" else "",
                              industry_desc=ind_desc if ind_key != "일반" else "")

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    # 첨부파일명 생성 (YYYYMM 형식)
    year_month   = datetime.now().strftime("%Y%m")
    txt_filename = f"60sec_econ_signal_{year_month}.txt"
    srt_filename = f"60sec_econ_signal_{year_month}.srt"

    # TXT / SRT 첨부가 하나라도 있으면 mixed 구조로 전환
    has_srt = attach_srt and srt_p.exists()
    if script_p.exists() or has_srt:
        outer = MIMEMultipart("mixed")
        outer["Subject"] = msg["Subject"]
        outer["From"]    = msg["From"]
        outer["To"]      = msg["To"]
        outer.attach(msg)   # alternative 파트 포함

        # TXT 스크립트 첨부
        txt_part = MIMEBase("text", "plain", charset="utf-8")
        txt_part.set_payload(script_p.read_bytes())
        encoders.encode_base64(txt_part)
        txt_part.add_header(
            "Content-Disposition",
            f'attachment; filename="{txt_filename}"',
        )
        outer.attach(txt_part)

        # SRT 자막 첨부 (옵션)
        if has_srt:
            with open(srt_p, "rb") as f:
                srt_part = MIMEBase("application", "octet-stream")
                srt_part.set_payload(f.read())
            encoders.encode_base64(srt_part)
            srt_part.add_header(
                "Content-Disposition",
                f'attachment; filename="{srt_filename}"',
            )
            outer.attach(srt_part)

        msg = outer

    # ── SMTP 발송 ───────────────────────────────────────────
    try:
        print(f"[emailer] SMTP 연결 중: {cfg['smtp_host']}:{cfg['smtp_port']}")
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_bytes())
        print(f"[emailer] ✓ 발송 완료 → {', '.join(recipients)}")
        return True

    except smtplib.SMTPAuthenticationError:
        print("[emailer] ✗ 인증 실패 — Gmail 앱 비밀번호를 사용하고 있는지 확인하세요.")
        print("  발급: Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호")
        return False
    except Exception as e:
        print(f"[emailer] ✗ 발송 실패: {e}")
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────
# 4. 거시지표 임계값 알림 (S2-4)
# ─────────────────────────────────────────────────────────────

# 알림 규칙 정의
# op: ">=" | "<=" | ">" | "<"
# level: "warning" | "danger"
# 같은 label에 여러 규칙이 걸릴 경우 severity가 가장 높은 규칙만 발송 (check_macro_alerts 내 dedup)
_ALERT_RULES: list[dict] = [
    {
        "label":     "환율(원/$)",
        "op":        ">=",
        "threshold": 1450,
        "level":     "warning",
        "msg":       "환율 경고 구간 진입 (1,450원/$ 이상)",
        "impact":    "수출 수금 환전 적기, 단 달러 결제 수입 원가 상승 주의",
    },
    {
        "label":     "환율(원/$)",
        "op":        ">=",
        "threshold": 1500,
        "level":     "danger",
        "msg":       "환율 위험 구간 진입 (1,500원/$ 이상)",
        "impact":    "달러 결제 부채·수입 원가 급증 — 즉각 헷징 여부 점검 필요",
    },
    {
        "label":     "소비자물가(CPI)",
        "op":        ">=",
        "threshold": 3.0,
        "level":     "danger",
        "msg":       "고물가 경고 (CPI 전년동월 +3.0% 이상)",
        "impact":    "원자재·운송비 비용 압박 심화 — 단가 재산정 긴급 검토",
    },
    {
        "label":     "수출증가율",
        "op":        "<=",
        "threshold": -10.0,
        "level":     "danger",
        "msg":       "수출 급감 경고 (전년동월 -10% 이상 하락)",
        "impact":    "주요 수출 시장 수요 급락 — 재고·생산 계획 긴급 점검",
    },
    {
        "label":     "수입물가지수",
        "op":        ">=",
        "threshold": 5.0,
        "level":     "danger",
        "msg":       "수입 원가 급등 경고 (전년동월 +5% 이상)",
        "impact":    "생산 원가 상승 압박 — 단가 전가 가능 여부 즉시 점검",
    },
    {
        "label":     "원/100엔 환율",
        "op":        "<=",
        "threshold": 800,
        "level":     "danger",
        "msg":       "극단적 엔저 경고 (원/100엔 800원 이하)",
        "impact":    "일본 경쟁 제품 가격 우위 강화 — 대일 수출 전략 재검토",
    },
]

_LEVEL_PRIORITY: dict[str, int] = {"normal": 0, "caution": 1, "warning": 2, "danger": 3}
_LEVEL_STYLE: dict[str, tuple] = {
    "warning": ("#fff3e0", "#f97316", "🔶"),
    "danger":  ("#ffeaea", "#ef4444", "🔴"),
}


def check_macro_alerts(macro: dict) -> list[dict]:
    """
    macro dict를 _ALERT_RULES에 대해 평가하여 조건을 만족하는 알림 목록을 반환한다.
    같은 label에 여러 규칙이 걸리면 가장 심각한(level 최고) 규칙만 남긴다.

    Args:
        macro: {label: {value, unit, trend, ...}} 형식의 거시지표 dict
               ('_'로 시작하는 메타 키는 미리 제거된 상태를 전제)

    Returns:
        [{"label", "value", "unit", "trend", "as_of", "note",
          "threshold", "op", "level", "msg", "impact"}, ...]
        임계값 초과 없으면 빈 리스트.
    """
    seen: dict[str, dict] = {}   # label → 가장 심각한 알림

    for rule in _ALERT_RULES:
        label = rule["label"]
        item  = macro.get(label, {})
        if not item:
            continue
        try:
            val = float(str(item.get("value", "")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue

        op, thr = rule["op"], rule["threshold"]
        if   op == ">=" and val >= thr:  hit = True
        elif op == "<=" and val <= thr:  hit = True
        elif op == ">"  and val >  thr:  hit = True
        elif op == "<"  and val <  thr:  hit = True
        else:                             hit = False

        if hit:
            alert = {
                "label":     label,
                "value":     item.get("value", ""),
                "unit":      item.get("unit", ""),
                "trend":     item.get("trend", ""),
                "as_of":     item.get("as_of", ""),
                "note":      item.get("note", ""),
                "threshold": thr,
                "op":        op,
                "level":     rule["level"],
                "msg":       rule["msg"],
                "impact":    rule["impact"],
            }
            # 같은 label이면 더 심각한 level로 교체
            if label not in seen:
                seen[label] = alert
            elif (_LEVEL_PRIORITY.get(alert["level"], 0)
                  > _LEVEL_PRIORITY.get(seen[label]["level"], 0)):
                seen[label] = alert

    return list(seen.values())


def _build_alert_html(alerts: list, macro: dict, sent_at: str) -> str:
    """임계값 알림 HTML 이메일 본문을 생성한다."""

    # 알림 카드
    alert_cards = ""
    for a in alerts:
        bg, border_color, icon = _LEVEL_STYLE.get(a["level"], ("#fffbeb", "#f59e0b", "⚠️"))
        thr_str = f"{a['threshold']:,}" if isinstance(a["threshold"], int) else str(a["threshold"])
        alert_cards += f"""
        <div style="background:{bg};border:2px solid {border_color};border-radius:8px;
                    padding:16px 20px;margin:10px 0">
          <div style="font-size:12px;font-weight:700;color:{border_color};margin-bottom:8px">
            {icon} {a['msg']}
          </div>
          <div style="font-size:22px;font-weight:900;color:#1a202c">
            {a['label']}: {a['value']}{a['unit']}
            <span style="font-size:16px;color:{border_color}">{a['trend']}</span>
          </div>
          <div style="font-size:11px;color:#666;margin-top:6px">
            임계값: {a['op']} {thr_str}{a['unit']} &nbsp;|&nbsp; {a['note']}
          </div>
          <div style="font-size:12px;color:#374151;margin-top:10px;
                      padding:8px 12px;background:rgba(255,255,255,0.75);border-radius:4px;
                      line-height:1.6">
            💡 <b>사업 영향:</b> {a['impact']}
          </div>
          <div style="font-size:10px;color:#9ca3af;margin-top:6px">기준일: {a['as_of']}</div>
        </div>"""

    # 전체 지표 현황 테이블
    alert_labels = {a["label"] for a in alerts}
    macro_rows = ""
    for label, d in macro.items():
        is_alert  = label in alert_labels
        val_style = "font-weight:800;color:#dc2626" if is_alert else "color:#374151"
        macro_rows += (
            f'<tr style="border-bottom:1px solid #f3f4f6">'
            f'<td style="padding:7px 8px;font-size:12px;color:#374151">{label}</td>'
            f'<td style="padding:7px 8px;font-size:12px;text-align:right;{val_style}">'
            f'{d.get("value","")}{d.get("unit","")} {d.get("trend","")}</td>'
            f'<td style="padding:7px 8px;font-size:11px;color:#9ca3af">{d.get("as_of","")}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fef2f2;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
<div style="max-width:580px;margin:28px auto;background:#fff;border-radius:12px;
            box-shadow:0 2px 16px rgba(239,68,68,.15)">

  <!-- 헤더 -->
  <div style="background:#7f1d1d;padding:24px 28px">
    <div style="font-size:11px;color:#fca5a5;letter-spacing:1.5px;margin-bottom:8px">
      MACRO ALERT · 자동 알림
    </div>
    <div style="font-size:22px;font-weight:900;color:#fff">
      ⚠️ 거시지표 임계값 알림
    </div>
    <div style="font-size:12px;color:#fca5a5;margin-top:6px">{sent_at} 발송</div>
  </div>

  <!-- 알림 카드 목록 -->
  <div style="padding:20px 28px">
    <div style="font-size:13px;font-weight:700;color:#1a202c;margin-bottom:4px">
      🚨 임계값 초과 지표 ({len(alerts)}개)
    </div>
    {alert_cards}
  </div>

  <!-- 전체 지표 현황 -->
  <div style="padding:16px 28px;border-top:1px solid #fee2e2">
    <div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:10px">
      📈 전체 거시지표 현황
    </div>
    <table style="width:100%;border-collapse:collapse">
      {macro_rows}
    </table>
  </div>

  <!-- 푸터 -->
  <div style="background:#fef2f2;padding:14px 28px;
              border-top:2px solid #fecaca;font-size:11px;color:#b91c1c">
    본 알림은 임계값 초과 시 자동 발송됩니다 &nbsp;|&nbsp; 출처: 한국은행 ECOS
    &nbsp;|&nbsp; 생성일: {datetime.now().strftime("%Y-%m-%d")}
  </div>
</div>
</body>
</html>"""


def send_alert_email(macro: "Optional[dict]" = None) -> bool:
    """
    거시지표 임계값 초과 알림 이메일을 발송한다.

    Args:
        macro: {label: {value, unit, ...}} 거시지표 dict.
               None이면 data/macro.json에서 자동 로드.

    Returns:
        True  — 임계값 초과 지표 있음 + 발송 성공
        False — 설정 없음 | 초과 지표 없음 | 발송 실패
    """
    if not is_configured():
        print("[alert] 이메일 설정 없음 — 알림 건너뜀")
        return False

    if macro is None:
        macro = _load_macro()

    if not macro:
        print("[alert] 거시지표 데이터 없음 — 알림 건너뜀")
        return False

    alerts = check_macro_alerts(macro)
    if not alerts:
        print("[alert] 임계값 초과 지표 없음 — 알림 발송 건너뜀")
        return False

    cfg        = _load_config()
    recipients = [r.strip() for r in cfg["recipients"].split(",") if r.strip()]
    sent_at    = datetime.now().strftime("%Y-%m-%d %H:%M KST")

    alert_labels = "·".join(a["label"] for a in alerts)
    subject      = f"⚠️ [거시지표 알림] {alert_labels} 임계값 초과"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("60초 경제신호", cfg["sender"]))
    msg["To"]      = ", ".join(recipients)

    # ── plain text ─────────────────────────────────────────
    plain_lines = [
        f"[거시지표 임계값 알림] {sent_at}",
        "=" * 50,
        f"총 {len(alerts)}개 지표 임계값 초과:",
        "",
    ]
    for a in alerts:
        plain_lines += [
            f"  [{a['level'].upper()}] {a['msg']}",
            f"    현재값: {a['label']} {a['value']}{a['unit']} {a['trend']}",
            f"    기준일: {a['as_of']}",
            f"    사업 영향: {a['impact']}",
            "",
        ]
    plain_lines += ["─" * 50, "전체 지표 현황:", ""]
    for label, d in macro.items():
        plain_lines.append(
            f"  {label}: {d.get('value','')}{d.get('unit','')} "
            f"{d.get('trend','')}  ({d.get('as_of','')})"
        )
    plain_lines += [
        "", "─" * 50,
        "본 알림은 임계값 초과 시 자동 발송됩니다. | 출처: 한국은행 ECOS",
    ]

    html_body  = _build_alert_html(alerts, macro, sent_at)
    plain_body = "\n".join(plain_lines)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    try:
        print(f"[alert] SMTP 연결 중: {cfg['smtp_host']}:{cfg['smtp_port']}")
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_bytes())
        print(f"[alert] ✓ 알림 발송 완료 → {', '.join(recipients)}")
        for a in alerts:
            print(f"  [{a['level'].upper()}] {a['label']}: {a['value']}{a['unit']}")
        return True

    except smtplib.SMTPAuthenticationError:
        print("[alert] ✗ 인증 실패 — Gmail 앱 비밀번호를 사용하고 있는지 확인하세요.")
        return False
    except Exception as e:
        print(f"[alert] ✗ 발송 실패: {e}")
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────
# 5. 대시보드 리포트 이메일 발송
# ─────────────────────────────────────────────────────────────

def send_report_email(
    html_body: str,
    subject: str,
    extra_recipients: Optional[list] = None,
) -> bool:
    """
    대시보드에서 생성한 HTML 리포트를 이메일로 발송한다.

    Args:
        html_body:        generate_report_html() 반환 HTML 문자열
        subject:          이메일 제목
        extra_recipients: 기본 수신자 외 추가 수신자 목록 (선택)

    Returns:
        True  — 발송 성공
        False — 설정 없음 또는 발송 실패
    """
    if not is_configured():
        print("[report_email] 이메일 설정 없음 — 발송 건너뜀")
        return False

    cfg = _load_config()
    base_recipients = [r.strip() for r in cfg["recipients"].split(",") if r.strip()]
    extra = [r.strip() for r in (extra_recipients or []) if r.strip()]
    # 중복 제거하면서 순서 유지
    seen: set = set()
    recipients: list = []
    for r in base_recipients + extra:
        if r not in seen:
            seen.add(r)
            recipients.append(r)

    if not recipients:
        print("[report_email] 수신자 없음 — 발송 건너뜀")
        return False

    # ── 간단한 plaintext fallback ────────────────────
    plain_body = (
        f"[대시보드 리포트]\n"
        f"HTML 형식 이메일입니다. HTML을 지원하는 메일 클라이언트에서 확인하세요.\n\n"
        f"발송일: {datetime.now().strftime('%Y-%m-%d %H:%M')} KST"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("60초 경제신호", cfg["sender"]))
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    try:
        print(f"[report_email] SMTP 연결 중: {cfg['smtp_host']}:{cfg['smtp_port']}")
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_bytes())
        print(f"[report_email] ✓ 발송 완료 → {', '.join(recipients)}")
        return True

    except smtplib.SMTPAuthenticationError:
        print("[report_email] ✗ 인증 실패 — Gmail 앱 비밀번호를 확인하세요.")
        return False
    except Exception as e:
        print(f"[report_email] ✗ 발송 실패: {e}")
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────
# 6. B2B 구독자 기반 산업별 발송 (T-11)
# ─────────────────────────────────────────────────────────────

def send_to_subscribers(industry: Optional[str] = None) -> dict:
    """
    구독자 DB 기반으로 산업별 이메일을 발송한다.

    Args:
        industry: 특정 산업만 발송 (None이면 모든 산업 순차 발송)

    Returns:
        {"sent": int, "failed": int, "skipped": int, "details": [...]}
    """
    from core.subscription import get_industry_send_list

    result = {"sent": 0, "failed": 0, "skipped": 0, "details": []}

    cfg = _load_config()
    if not cfg["sender"] or not cfg["password"]:
        print("[subscriber] 이메일 설정(sender/password) 없음 — 발송 건너뜀")
        result["details"].append("이메일 설정 없음")
        return result

    send_list = get_industry_send_list()
    if industry:
        send_list = {industry: send_list.get(industry, [])}

    for ind, emails in send_list.items():
        if not emails:
            result["skipped"] += 1
            result["details"].append(f"{ind}: 구독자 없음 — 건너뜀")
            continue

        # 산업 환경변수 임시 설정
        prev_industry = os.environ.get("INDUSTRY", "")
        prev_recipients = os.environ.get("EMAIL_RECIPIENTS", "")
        try:
            os.environ["INDUSTRY"] = ind
            os.environ["EMAIL_RECIPIENTS"] = ",".join(emails)
            ok = send_script_email()
            if ok:
                result["sent"] += len(emails)
                result["details"].append(f"{ind}: {len(emails)}명 발송 완료")
            else:
                result["failed"] += len(emails)
                result["details"].append(f"{ind}: 발송 실패")
        except Exception as e:
            result["failed"] += len(emails)
            result["details"].append(f"{ind}: 오류 — {e}")
        finally:
            # 환경변수 복원
            if prev_industry:
                os.environ["INDUSTRY"] = prev_industry
            elif "INDUSTRY" in os.environ:
                del os.environ["INDUSTRY"]
            if prev_recipients:
                os.environ["EMAIL_RECIPIENTS"] = prev_recipients
            elif "EMAIL_RECIPIENTS" in os.environ:
                del os.environ["EMAIL_RECIPIENTS"]

    print(f"[subscriber] 발송 결과: sent={result['sent']}, failed={result['failed']}, skipped={result['skipped']}")
    return result


# ─────────────────────────────────────────────────────────────
# CLI: python -m core.emailer
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys as _sys
    print("=== 이메일 발송 테스트 ===")
    if not is_configured():
        print("[경고] 이메일 설정이 없습니다.")
        print("  필요한 환경변수:")
        print("    EMAIL_SENDER      발신자 Gmail 주소")
        print("    EMAIL_PASSWORD    Gmail 앱 비밀번호 (16자리)")
        print("    EMAIL_RECIPIENTS  수신자 주소 (쉼표 구분)")
        _sys.exit(1)

    import argparse
    parser = argparse.ArgumentParser(description="이메일 발송 테스트")
    parser.add_argument("--alert", action="store_true", help="알림 이메일 테스트 (임계값 강제 발송)")
    args = parser.parse_args()

    if args.alert:
        print("\n[알림 이메일 테스트]")
        _macro = _load_macro()
        _alerts = check_macro_alerts(_macro)
        if _alerts:
            print(f"임계값 초과 지표 {len(_alerts)}개:")
            for a in _alerts:
                print(f"  [{a['level']}] {a['msg']} — 현재값: {a['value']}{a['unit']}")
            ok = send_alert_email(macro=_macro)
        else:
            print("현재 임계값 초과 지표 없음 — 테스트용 강제 발송을 건너뜀")
            print("실제 지표가 임계값을 초과했을 때 자동 발송됩니다.")
            ok = True
        _sys.exit(0 if ok else 1)
    else:
        print("\n[스크립트 이메일 테스트]")
        ok = send_script_email()
        _sys.exit(0 if ok else 1)
