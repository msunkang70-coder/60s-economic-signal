"""
core/auto_pipeline.py
실시간 데이터 자동 갱신 파이프라인

run_daily_pipeline():
  1. ECOS 7개 지표 수집 (macro.json 갱신)
  2. 이상치 탐지 (Z-score + IQR)
  3. 이상치 발견 시 알림 발송
  최대 3회 재시도 (exponential backoff)
"""

from __future__ import annotations

import json
import pathlib
import time
import traceback
from datetime import datetime
from typing import Optional

_ROOT = pathlib.Path(__file__).parent.parent
_PIPELINE_LOG_PATH = _ROOT / "data" / "pipeline_log.jsonl"

_MAX_RETRIES = 3
_BASE_BACKOFF = 2  # seconds


def _log_pipeline(event: str, detail: Optional[dict] = None) -> None:
    """파이프라인 이벤트를 JSONL로 로깅한다."""
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
    }
    if detail:
        record["detail"] = detail

    _PIPELINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PIPELINE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_daily_pipeline(api_key: Optional[str] = None) -> dict:
    """일일 데이터 갱신 파이프라인을 실행한다.

    Steps:
      1. ECOS 지표 수집 (refresh_macro)
      2. 이상치 탐지 (detect_anomalies)
      3. 이상치 알림 발송

    최대 3회 재시도 (exponential backoff: 2s, 4s, 8s)

    Returns:
        {"status": "ok"|"error", "indicators": int,
         "anomalies": int, "retries": int, "error": str|None}
    """
    _log_pipeline("pipeline_start")

    result = {
        "status": "error",
        "indicators": 0,
        "anomalies": 0,
        "retries": 0,
        "error": None,
    }

    macro_data = None

    # Step 1: ECOS 수집 (재시도 포함)
    for attempt in range(_MAX_RETRIES):
        try:
            from core.ecos import refresh_macro
            macro_data = refresh_macro(api_key=api_key)
            indicator_count = sum(
                1 for k in macro_data if not k.startswith("_")
            )
            result["indicators"] = indicator_count
            _log_pipeline("ecos_refresh_ok", {"indicators": indicator_count})
            break
        except Exception as e:
            result["retries"] = attempt + 1
            wait = _BASE_BACKOFF * (2 ** attempt)
            _log_pipeline("ecos_refresh_retry", {
                "attempt": attempt + 1,
                "error": str(e),
                "wait_seconds": wait,
            })
            if attempt < _MAX_RETRIES - 1:
                time.sleep(wait)
            else:
                result["error"] = f"ECOS refresh failed after {_MAX_RETRIES} retries: {e}"
                _log_pipeline("ecos_refresh_fail", {"error": str(e)})
                return result

    if not macro_data:
        result["error"] = "No macro data returned"
        return result

    # Step 2: 이상치 탐지
    try:
        from core.anomaly_detector import detect_anomalies, save_anomaly_log
        anomalies = detect_anomalies(macro_data)
        result["anomalies"] = len(anomalies)

        if anomalies:
            save_anomaly_log(anomalies)
            _log_pipeline("anomalies_detected", {
                "count": len(anomalies),
                "indicators": [a["indicator"] for a in anomalies],
            })

            # Step 3: 이상치 알림 (critical/warning만)
            _send_anomaly_alerts(anomalies)

            # Step 3b: pipeline_notifier 이중 알림 (이메일 + Slack)
            try:
                from core.pipeline_notifier import send_anomaly_alert
                send_anomaly_alert(anomalies)
            except Exception as notify_err:
                _log_pipeline("notifier_alert_error", {"error": str(notify_err)})
    except Exception as e:
        _log_pipeline("anomaly_detection_error", {"error": str(e)})
        traceback.print_exc()

    result["status"] = "ok"
    _log_pipeline("pipeline_complete", {
        "indicators": result["indicators"],
        "anomalies": result["anomalies"],
    })
    return result


def _send_anomaly_alerts(anomalies: list[dict]) -> None:
    """critical/warning 이상치에 대해 알림을 발송한다."""
    alerts = [a for a in anomalies if a.get("severity") in ("critical", "warning")]
    if not alerts:
        return

    try:
        from core.alert_channels import load_channel_config, route_alert
        cfg = load_channel_config()

        triggered_items = []
        for a in alerts:
            triggered_items.append({
                "indicator": a["indicator"],
                "condition": "anomaly",
                "threshold": f"Z={a['z_score']}",
                "current_value": a["value"],
                "unit": a.get("unit", ""),
                "trend": "⚠",
                "industry_keys": [],
                "notify_email": True,
            })

        if triggered_items:
            route_alert(triggered_items, cfg)
            _log_pipeline("anomaly_alerts_sent", {"count": len(triggered_items)})
    except Exception as e:
        _log_pipeline("anomaly_alert_error", {"error": str(e)})


def on_refresh_complete(macro_data: dict) -> None:
    """ecos.py refresh_macro() 완료 후 호출되는 훅.

    이상치 탐지만 수행 (파이프라인 전체 재실행 방지).
    """
    try:
        from core.anomaly_detector import detect_anomalies, save_anomaly_log
        anomalies = detect_anomalies(macro_data)
        if anomalies:
            save_anomaly_log(anomalies)
            _send_anomaly_alerts(anomalies)
    except Exception:
        pass


def get_pipeline_status() -> dict:
    """최근 파이프라인 실행 상태를 반환한다."""
    if not _PIPELINE_LOG_PATH.exists():
        return {"last_run": None, "status": "never"}

    try:
        lines = _PIPELINE_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
        if not lines:
            return {"last_run": None, "status": "never"}

        # 마지막 complete/fail 이벤트 찾기
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                if entry.get("event") in ("pipeline_complete", "ecos_refresh_fail"):
                    return {
                        "last_run": entry.get("timestamp"),
                        "status": "ok" if entry["event"] == "pipeline_complete" else "error",
                        "detail": entry.get("detail", {}),
                    }
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    return {"last_run": None, "status": "unknown"}


# ══════════════════════════════════════════════════════════
# Performance & Pipeline — Agent 5
# ══════════════════════════════════════════════════════════


class _CircuitBreaker:
    """간단한 서킷 브레이커.

    연속 실패가 failure_threshold에 도달하면 OPEN 상태로 전환.
    reset_timeout(초) 경과 후 HALF_OPEN → 성공 시 CLOSED.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 3, reset_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._state = self.CLOSED
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.time() - self._last_failure_time >= self.reset_timeout:
                self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN

    def allow_request(self) -> bool:
        s = self.state
        return s in (self.CLOSED, self.HALF_OPEN)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "reset_timeout": self.reset_timeout,
        }


def run_health_check() -> dict:
    """파이프라인 의존 모듈 가용성 체크.

    Returns:
        {"status": "ok"|"degraded"|"down",
         "checks": {module_name: bool, ...}}
    """
    checks: dict[str, bool] = {}
    modules = [
        ("ecos", "core.ecos"),
        ("anomaly_detector", "core.anomaly_detector"),
        ("alert_channels", "core.alert_channels"),
        ("pipeline_notifier", "core.pipeline_notifier"),
        ("data_freshness", "core.data_freshness"),
    ]
    for name, mod_path in modules:
        try:
            __import__(mod_path)
            checks[name] = True
        except Exception:
            checks[name] = False

    # macro.json 존재 여부
    macro_path = _ROOT / "data" / "macro.json"
    checks["macro_json"] = macro_path.exists()

    ok_count = sum(1 for v in checks.values() if v)
    total = len(checks)
    if ok_count == total:
        status = "ok"
    elif ok_count == 0:
        status = "down"
    else:
        status = "degraded"

    return {"status": status, "checks": checks}


def get_pipeline_metrics(days: int = 7) -> dict:
    """최근 N일간 pipeline_log.jsonl 이벤트를 집계.

    Returns:
        {"total_events": int, "events": {event_name: count},
         "last_success": str|None, "last_error": str|None, "days": int}
    """
    result: dict = {
        "total_events": 0,
        "events": {},
        "last_success": None,
        "last_error": None,
        "days": days,
    }

    if not _PIPELINE_LOG_PATH.exists():
        return result

    try:
        cutoff = datetime.now() - __import__("datetime").timedelta(days=days)
        lines = _PIPELINE_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            if ts < cutoff:
                continue

            result["total_events"] += 1
            evt = entry.get("event", "unknown")
            result["events"][evt] = result["events"].get(evt, 0) + 1

            if evt == "pipeline_complete":
                result["last_success"] = ts_str
            elif evt in ("ecos_refresh_fail", "anomaly_alert_error"):
                result["last_error"] = ts_str
    except Exception:
        pass

    return result
