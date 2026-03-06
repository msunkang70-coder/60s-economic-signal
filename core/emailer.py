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
from typing import Optional

_ROOT = pathlib.Path(__file__).parent.parent


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


def _build_html(script_text: str, macro: dict, issue_month: str) -> str:
    """60초 스크립트 + 거시지표를 담은 HTML 이메일 본문을 생성한다."""

    # 거시지표 카드 HTML
    macro_cards = ""
    for label, d in macro.items():
        trend = d.get("trend", "")
        trend_color = "#e53e3e" if trend == "▲" else ("#2d9b4e" if trend == "▼" else "#888")
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
          <div style="font-size:10px;color:#aaa;margin-top:4px">기준일: {d.get("as_of","")}</div>
        </div>"""

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
        elif stripped.startswith("※"):
            # 참고 기사 이후 — 별도 처리
            script_html += f'<div style="color:#aaa;font-size:11px">{stripped}</div>'
        elif stripped.startswith("---"):
            script_html += '<hr style="border:none;border-top:1px solid #e2e8f0;margin:10px 0">'
        else:
            script_html += f'<p style="margin:4px 0;line-height:1.8;color:#444">{stripped}</p>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
<div style="max-width:640px;margin:32px auto;background:#fff;border-radius:12px;
            box-shadow:0 2px 16px rgba(0,0,0,.08);overflow:hidden">

  <!-- 헤더 -->
  <div style="background:#1a202c;padding:28px 32px">
    <div style="font-size:11px;color:#90cdf4;letter-spacing:1px;margin-bottom:6px">
      MONTHLY ECONOMIC SIGNAL
    </div>
    <div style="font-size:22px;font-weight:900;color:#fff">
      📊 60초 경제신호 — {issue_month}
    </div>
    <div style="font-size:12px;color:#a0aec0;margin-top:6px">
      매월 KDI 나라경제 이슈를 60초로 요약합니다
    </div>
  </div>

  <!-- 거시지표 -->
  <div style="padding:24px 32px;border-bottom:1px solid #e2e8f0">
    <div style="font-size:13px;font-weight:700;color:#1a202c;margin-bottom:12px">
      📈 이번 달 주요 거시지표
    </div>
    <div>{macro_cards}</div>
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


def _build_plain(script_text: str, macro: dict, issue_month: str) -> str:
    """HTML을 지원하지 않는 클라이언트용 plaintext 본문."""
    lines = [
        f"[60초 경제신호 — {issue_month}]",
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

    # ── 이메일 조립 ─────────────────────────────────────────
    recipients = [r.strip() for r in cfg["recipients"].split(",") if r.strip()]
    subject    = f"📊 [{issue_month}] 60초 경제신호"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender"]
    msg["To"]      = ", ".join(recipients)

    plain_body = _build_plain(script_text, macro, issue_month)
    html_body  = _build_html(script_text, macro, issue_month)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    # SRT 파일 첨부 (옵션)
    if attach_srt and srt_p.exists():
        outer = MIMEMultipart("mixed")
        outer["Subject"] = msg["Subject"]
        outer["From"]    = msg["From"]
        outer["To"]      = msg["To"]
        outer.attach(msg)   # alternative 파트 포함

        with open(srt_p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{issue_month.replace(" ", "_")}_경제신호.srt"',
        )
        outer.attach(part)
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
            box-shadow:0 2px 16px rgba(239,68,68,.15);overflow:hidden">

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
    msg["From"]    = cfg["sender"]
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
