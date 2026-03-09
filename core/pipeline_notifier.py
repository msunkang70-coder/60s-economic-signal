"""
core/pipeline_notifier.py
파이프라인 완료 알림 — Slack + 이메일 + JSONL 로깅

GitHub Actions의 notify job에서 실행되거나,
로컬에서 직접 호출할 수 있다.

환경변수:
  SLACK_WEBHOOK_URL   Slack Incoming Webhook URL (선택)
  ADMIN_EMAIL         관리자 이메일 (선택, EMAIL_SENDER/PASSWORD 필요)
"""

import json
import os
import pathlib
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

_ROOT = pathlib.Path(__file__).parent.parent
_LOG_PATH = _ROOT / "data" / "pipeline_log.jsonl"

_INDUSTRIES = ["반도체", "자동차", "화학", "소비재", "배터리", "조선", "철강", "일반"]


# ─────────────────────────────────────────────────────────────
# 1. 파이프라인 로그 (JSONL)
# ─────────────────────────────────────────────────────────────

def log_pipeline_result(
    industry: str,
    status: str,
    duration: float = 0.0,
) -> None:
    """
    파이프라인 실행 결과를 data/pipeline_log.jsonl에 append한다.

    Args:
        industry: 산업 키
        status: "success" | "failure" | "cancelled" 등
        duration: 소요 시간(초)
    """
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industry": industry,
        "status": status,
        "duration_sec": round(duration, 2),
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[pipeline] 로그 기록: {industry} → {status} ({duration:.1f}s)")


def _load_latest_results() -> list[dict]:
    """
    pipeline_log.jsonl에서 최근 실행의 산업별 결과를 읽는다.
    오늘 날짜 기준으로 가장 마지막 기록을 산업별로 1건씩 반환.
    """
    if not _LOG_PATH.exists():
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    latest: dict[str, dict] = {}

    with open(_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("ts", "").startswith(today):
                latest[entry["industry"]] = entry

    return list(latest.values())


# ─────────────────────────────────────────────────────────────
# 2. Slack 알림
# ─────────────────────────────────────────────────────────────

def send_slack_notification(results: list[dict]) -> None:
    """
    Slack Incoming Webhook으로 파이프라인 완료 알림을 보낸다.

    Args:
        results: [{"industry", "status", "duration_sec", "ts"}, ...]
    """
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[pipeline] SLACK_WEBHOOK_URL 없음 — Slack 알림 건너뜀")
        return

    now = datetime.now()
    month_label = now.strftime("%Y-%m")
    total = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    failed = total - success
    total_sec = sum(r.get("duration_sec", 0) for r in results)
    total_min = int(total_sec // 60)
    total_rem = int(total_sec % 60)

    # 산업별 상태 라인
    status_parts = []
    for ind in _INDUSTRIES:
        r = next((r for r in results if r.get("industry") == ind), None)
        if r:
            icon = "✅" if r.get("status") == "success" else "❌"
            status_parts.append(f"{ind} {icon}")
        else:
            status_parts.append(f"{ind} ⏭️")
    status_line = " | ".join(status_parts)

    # 헤더 이모지
    header_icon = "✅" if failed == 0 else "⚠️"
    header_text = "파이프라인 완료" if failed == 0 else f"파이프라인 완료 (실패 {failed}건)"

    text = (
        f"{header_icon} *60초 경제신호 파이프라인 {header_text}*\n"
        f"📅 {month_label} | {total}개 산업 처리\n"
        f"━━━━━━━━━━━━━━\n"
        f"{status_line}\n"
        f"━━━━━━━━━━━━━━\n"
        f"총 소요시간: {total_min}분 {total_rem}초"
    )

    import requests
    try:
        resp = requests.post(
            webhook_url,
            json={"text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            print("[pipeline] ✓ Slack 알림 발송 완료")
        else:
            print(f"[pipeline] Slack 응답 오류: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[pipeline] Slack 발송 실패: {e}")


# ─────────────────────────────────────────────────────────────
# 3. 관리자 이메일 알림
# ─────────────────────────────────────────────────────────────

def send_summary_email(results: list[dict]) -> None:
    """
    관리자에게 파이프라인 완료 요약 이메일을 발송한다.

    환경변수:
        ADMIN_EMAIL      관리자 수신 주소
        EMAIL_SENDER     발신 주소
        EMAIL_PASSWORD   앱 비밀번호
    """
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    sender = os.environ.get("EMAIL_SENDER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()

    if not admin_email or not sender or not password:
        print("[pipeline] ADMIN_EMAIL/EMAIL_SENDER/PASSWORD 없음 — 이메일 알림 건너뜀")
        return

    now = datetime.now()
    month_label = now.strftime("%Y-%m")
    total = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    failed = total - success
    total_sec = sum(r.get("duration_sec", 0) for r in results)

    # 산업별 행
    rows_html = ""
    for ind in _INDUSTRIES:
        r = next((r for r in results if r.get("industry") == ind), None)
        if r:
            status = r.get("status", "unknown")
            if status == "success":
                badge = '<span style="color:#16a34a;font-weight:700">✅ 성공</span>'
            else:
                badge = f'<span style="color:#dc2626;font-weight:700">❌ {status}</span>'
            dur = f'{r.get("duration_sec", 0):.0f}초'
        else:
            badge = '<span style="color:#9ca3af">⏭️ 미실행</span>'
            dur = "—"
        rows_html += (
            f'<tr style="border-bottom:1px solid #f3f4f6">'
            f'<td style="padding:8px 12px;font-size:13px">{ind}</td>'
            f'<td style="padding:8px 12px;font-size:13px;text-align:center">{badge}</td>'
            f'<td style="padding:8px 12px;font-size:13px;text-align:right;color:#6b7280">{dur}</td>'
            f'</tr>'
        )

    header_icon = "✅" if failed == 0 else "⚠️"
    header_bg = "#065f46" if failed == 0 else "#7f1d1d"
    subject = f"{header_icon} [파이프라인] {month_label} — {total}개 산업 {'완료' if failed == 0 else f'완료 (실패 {failed}건)'}"

    html_body = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
<div style="max-width:540px;margin:28px auto;background:#fff;border-radius:12px;
            box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:{header_bg};padding:24px 28px;border-radius:12px 12px 0 0">
    <div style="font-size:11px;color:rgba(255,255,255,.7);letter-spacing:1.5px;margin-bottom:6px">
      PIPELINE SUMMARY
    </div>
    <div style="font-size:20px;font-weight:900;color:#fff">
      {header_icon} 60초 경제신호 파이프라인 완료
    </div>
    <div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:6px">
      {month_label} · {now.strftime("%Y-%m-%d %H:%M")} KST
    </div>
  </div>

  <div style="padding:20px 28px">
    <div style="display:flex;gap:16px;margin-bottom:20px">
      <div style="flex:1;text-align:center;padding:12px;background:#f0fdf4;border-radius:8px">
        <div style="font-size:24px;font-weight:900;color:#16a34a">{success}</div>
        <div style="font-size:11px;color:#6b7280">성공</div>
      </div>
      <div style="flex:1;text-align:center;padding:12px;background:{'#fef2f2' if failed else '#f9fafb'};border-radius:8px">
        <div style="font-size:24px;font-weight:900;color:{'#dc2626' if failed else '#9ca3af'}">{failed}</div>
        <div style="font-size:11px;color:#6b7280">실패</div>
      </div>
      <div style="flex:1;text-align:center;padding:12px;background:#f9fafb;border-radius:8px">
        <div style="font-size:24px;font-weight:900;color:#374151">{total_sec:.0f}s</div>
        <div style="font-size:11px;color:#6b7280">총 소요</div>
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">
          <th style="padding:8px 12px;font-size:12px;text-align:left;color:#6b7280">산업</th>
          <th style="padding:8px 12px;font-size:12px;text-align:center;color:#6b7280">상태</th>
          <th style="padding:8px 12px;font-size:12px;text-align:right;color:#6b7280">소요</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <div style="background:#f8fafc;padding:14px 28px;border-top:1px solid #e2e8f0;
              font-size:11px;color:#9ca3af;border-radius:0 0 12px 12px">
    GitHub Actions 자동 발송 · {now.strftime("%Y-%m-%d %H:%M:%S")} KST
  </div>
</div>
</body>
</html>"""

    plain_body = (
        f"[파이프라인 완료] {month_label}\n"
        f"성공: {success} / 실패: {failed} / 총 소요: {total_sec:.0f}초\n\n"
        + "\n".join(
            f"  {ind}: {next((r.get('status','?') for r in results if r.get('industry')==ind), '미실행')}"
            for ind in _INDUSTRIES
        )
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("60초 경제신호", sender))
    msg["To"] = admin_email
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))

    try:
        print(f"[pipeline] 관리자 이메일 발송 중: {admin_email}")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [admin_email], msg.as_bytes())
        print(f"[pipeline] ✓ 관리자 이메일 발송 완료 → {admin_email}")
    except Exception as e:
        print(f"[pipeline] ✗ 이메일 발송 실패: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────
# CLI: python core/pipeline_notifier.py
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 파이프라인 완료 알림 ===")

    results = _load_latest_results()
    if not results:
        # 로그가 없으면 전체 산업 성공으로 가정 (GitHub Actions에서 호출 시)
        print("[pipeline] 오늘 로그 없음 — 전체 성공으로 간주")
        results = [
            {"industry": ind, "status": "success", "duration_sec": 0, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            for ind in _INDUSTRIES
        ]

    total = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    failed = total - success

    print(f"\n결과: {total}개 산업 — 성공 {success} / 실패 {failed}")
    for r in results:
        icon = "✅" if r.get("status") == "success" else "❌"
        print(f"  {icon} {r['industry']}: {r.get('status', '?')} ({r.get('duration_sec', 0):.0f}s)")

    send_slack_notification(results)
    send_summary_email(results)

    print("\n=== 알림 처리 완료 ===")
