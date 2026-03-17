"""
core/ecos.py
한국은행 ECOS Open API 연동 — 거시지표 자동 수집 & macro.json 갱신

API 키 설정 방법 (우선순위):
  1. 환경변수:   ECOS_API_KEY=<your_key>
  2. Streamlit:  .streamlit/secrets.toml
                   [ecos]
                   api_key = "<your_key>"
  3. 키 없음:   기존 macro.json 유지 (Backward Compatible)

무료 API 키 발급: https://ecos.bok.or.kr → 오픈 API → 인증키 신청

CLI 실행 (즉시 갱신):
  python -m core.ecos
"""

import json
import os
import pathlib
import tempfile
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

# ─── 경로 설정 ────────────────────────────────────────────────
_ROOT      = pathlib.Path(__file__).parent.parent   # 프로젝트 루트
MACRO_PATH = _ROOT / "data" / "macro.json"
ECOS_BASE  = "https://ecos.bok.or.kr/api"
_TIMEOUT   = 10
_MAX_RETRY = 2   # 최초 1회 + 재시도 최대 2회
_RETRY_WAIT = 2  # 재시도 대기 초

# ─── ECOS 지표 정의 ───────────────────────────────────────────
# stat_code / item_code 는 ECOS Open API 명세 기준
# yoy=True  → 전년동월 대비 YoY 증가율 계산 (CPI, 수출 등)
# yoy=False → 최신 절댓값 그대로 사용 (기준금리 등)
# 코드 변경 시 이 dict만 수정하면 됨
_SPEC: dict = {
    "환율(원/$)": {
        # 수정: 036Y001(존재하지 않음) → 731Y003 (서울외환시장 일별 외환시세)
        # item_code 0000003 = 원/달러 종가(15:30 마감 매매기준율)
        # StatisticItemList/sample 로 검증 완료 (2026-03-06)
        "stat_code": "731Y003",
        "item_code": "0000003",   # 원/달러 종가(15:30)
        "period":    "D",
        "unit":      "원/$",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "일간",
    },
    "소비자물가(CPI)": {
        # 수정: 021Y125(존재하지 않음) → 901Y009 (소비자물가지수, 통계청 승인)
        # item_code "0" = 총지수, 월별 196501~최신
        # StatisticSearch/sample 실데이터 검증 완료 (2026-03-06)
        "stat_code": "901Y009",
        "item_code": "0",         # 총지수
        "period":    "M",
        "yoy":       True,
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "수출증가율": {
        # 한국은행 ECOS 수출금액지수(403Y001) YoY 기준
        # 관세청 통관기준 수출증가율과 최대 ±2~3%p 괴리 가능 (지수 산정 방식 차이)
        # 괴리가 크면 stat_code를 "301Y017"로 교체 후 재검증 필요
        "stat_code": "403Y001",
        "item_code": "*AA",       # 총지수
        "period":    "M",
        "yoy":       True,
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "기준금리": {
        "stat_code": "722Y001",   # 한국은행 기준금리 및 여수신금리
        "item_code": "0101000",   # 한국은행 기준금리
        "period":    "M",
        "yoy":       False,       # 절댓값 사용 (YoY 아님)
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "비정기",
    },
    "원/100엔 환율": {
        # 731Y004: 주요국 통화의 월별 원화환산 외환시세
        # item_code 0000002 = 원/100엔 (기간평균)
        # dedup=True: 같은 TIME에 기간말(0000100)·기간평균(0000200) 2행 반환
        #             → _dedup_by_time() 으로 기간평균(ITEM_CODE2=0000200) 보존
        # S2-3 검증: 202502 기준 val=961.82원/100엔 (2026-03-06 확인)
        "stat_code": "731Y004",
        "item_code": "0000002",
        "period":    "M",
        "yoy":       False,    # 절댓값 (전년동월비 아님)
        "dedup":     True,     # TIME 중복 제거 필요
        "unit":      "원/100엔",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "수출물가지수": {
        # 403Y002: 수출물가지수(2015=100), *AA=전체 총지수
        # YoY 기반: 전년동월 대비 수출 가격 변화율
        # S2-3 검증: 202502 기준 val=111.74, YoY≈-10.6% (2026-03-06 확인)
        "stat_code": "403Y002",
        "item_code": "*AA",
        "period":    "M",
        "yoy":       True,
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "수입물가지수": {
        # 403Y004: 수입물가지수(2015=100), *AA=전체 총지수
        # YoY 기반: 전년동월 대비 수입 가격 변화율
        # S2-3 검증: 202502 기준 val=103.02, YoY≈-4.0% (2026-03-06 확인)
        "stat_code": "403Y004",
        "item_code": "*AA",
        "period":    "M",
        "yoy":       True,
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "경상수지(억달러)": {
        # 056Y001: 국제수지(BPM6) — 경상수지
        # item_code "10101" = 경상수지 (백만달러 단위 → 억달러 변환)
        # 월별 절댓값 사용, YoY 아님
        "stat_code": "056Y001",
        "item_code": "10101",
        "period":    "M",
        "yoy":       False,
        "unit":      "억달러",
        "scale":     0.01,       # 백만달러 → 억달러 (÷100)
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "월간",
    },
    "GDP성장률": {
        # 111Y002: 국내총생산(지출항목별, 실질, 계절조정, 전기대비)
        # item_code "10101" = GDP 전기비 성장률 (%)
        # 분기별 절댓값 사용
        "stat_code": "111Y002",
        "item_code": "10101",
        "period":    "Q",
        "yoy":       False,
        "unit":      "%",
        "source_name": "한국은행 ECOS",
        "source_url":  "https://ecos.bok.or.kr/",
        "frequency":   "분기",
    },
}

_HEADERS = {"User-Agent": "60sec-econ-signal/1.0"}


# ─────────────────────────────────────────────────────────────
# 1. API 키 조회
# ─────────────────────────────────────────────────────────────
def _get_api_key() -> Optional[str]:
    """환경변수 → Streamlit secrets 순서로 ECOS API 키를 반환한다."""
    # 1) 환경변수
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if key:
        return key
    # 2) Streamlit secrets (app.py 컨텍스트에서만 유효)
    try:
        import streamlit as st
        key = (st.secrets.get("ecos") or {}).get("api_key", "").strip()
        if key:
            return key
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# 2. 날짜 범위 생성
# ─────────────────────────────────────────────────────────────
def _date_range(period: str, lookback: int = 14) -> tuple:
    """
    period='D' → 최근 lookback일 (YYYYMMDD)
    period='M' → 최근 lookback+2개월 (YYYYMM), 전년동월 대비 계산용 여유 포함
    period='Q' → 최근 lookback 분기 (YYYYQ1~Q4)
    """
    today = datetime.today()
    if period == "D":
        start = today - timedelta(days=lookback)
        return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")

    if period == "Q":
        # 분기: 최근 lookback 분기
        cur_q = (today.month - 1) // 3 + 1
        total_q = today.year * 4 + cur_q - lookback
        sy = (total_q - 1) // 4
        sq = (total_q - 1) % 4 + 1
        return f"{sy}Q{sq}", f"{today.year}Q{cur_q}"

    # Monthly: lookback개월 + 2개월 여유 (14개월분 → 전년동월 포함)
    n = lookback + 2
    total = today.year * 12 + (today.month - 1) - n
    sy, sm = divmod(total, 12)
    sm += 1
    return f"{sy}{sm:02d}", today.strftime("%Y%m")


# ─────────────────────────────────────────────────────────────
# 3. ECOS API 호출 (재시도 포함)
# ─────────────────────────────────────────────────────────────
def _fetch_rows(api_key: str, stat_code: str, item_code: str, period: str) -> list:
    """
    ECOS StatisticSearch API 호출 → 유효한 row 목록 반환.
    최초 1회 + 재시도 최대 2회 (2초 대기), 모두 실패 시 빈 리스트 반환.

    디버그 모드: 환경변수 ECOS_DEBUG=1 시 요청 URL·응답 상세 출력.
    일별(D) 지표는 lookback=30일, 월별(M)은 14+2=16개월.
    """
    # 일별은 주말·공휴일 고려해 30일 조회
    # 월별은 30개월 조회 → _yoy()가 rows[-2]~rows[-15] 필요하므로 여유 확보
    lookback = 30
    start, end = _date_range(period, lookback)

    url = (
        f"{ECOS_BASE}/StatisticSearch"
        f"/{api_key}/json/kr/1/100"
        f"/{stat_code}/{period}/{start}/{end}/{item_code}"
    )

    # 디버그 로깅 — API 키는 마스킹
    _debug = os.environ.get("ECOS_DEBUG", "").strip() == "1"
    if _debug:
        safe_url = url.replace(api_key, "***KEY***")
        print(f"[ECOS][DEBUG] {stat_code}/{item_code} ({period}) "
              f"range={start}~{end}  URL: {safe_url}")

    body = None
    for attempt in range(_MAX_RETRY + 1):   # 0, 1, 2
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            break   # 성공 시 루프 탈출
        except Exception as e:
            if attempt < _MAX_RETRY:
                print(f"[ECOS] 재시도 {attempt + 1}/{_MAX_RETRY} ({stat_code}/{item_code}): {e}")
                time.sleep(_RETRY_WAIT)
            else:
                print(f"[ECOS] 요청 실패 ({stat_code}/{item_code}): {e}")
                return []

    if body is None:
        return []

    # API 오류 응답 확인
    if "RESULT" in body:
        result = body["RESULT"]
        msg    = result.get("MESSAGE", result.get("CODE", ""))
        print(f"[ECOS] API 오류 ({stat_code}/{item_code}): {msg}")
        if _debug:
            print(f"[ECOS][DEBUG] 응답 전문: {json.dumps(body, ensure_ascii=False)[:400]}")
        return []

    rows = body.get("StatisticSearch", {}).get("row", [])
    if _debug:
        total = body.get("StatisticSearch", {}).get("list_total_count", "?")
        print(f"[ECOS][DEBUG] {stat_code}/{item_code}: {len(rows)}행 반환 (전체 {total}건)")
        if rows:
            r0 = rows[-1]
            print(f"[ECOS][DEBUG]   최신 샘플: TIME={r0.get('TIME')} VALUE={r0.get('DATA_VALUE')}")

    # DATA_VALUE가 빈 값인 행 제외
    return [r for r in rows if r.get("DATA_VALUE") not in (None, "", "-", "0")]


# ─────────────────────────────────────────────────────────────
# 4. 전년동월 대비 증가율 계산 (월별 공통)
# ─────────────────────────────────────────────────────────────
def _yoy(rows: list) -> tuple:
    """
    월별 rows에서 전년동월 대비 YoY 증가율(%) 계산.
    rows: TIME 오름차순 정렬된 dict 목록 (YYYYMM 형식 TIME 키)

    ※ rows[-2](최신 확정치) 기준 계산:
      - rows[-1]은 잠정치이거나 1월 저기저 등 이상치가 섞일 수 있음
      - rows[-2] ~ rows[-14] 비교 → 안정적인 전년동월비 제공
      - 최소 14행 필요 (30개월 lookback 시 항상 충족)

    Returns:
        (current_yoy: str, prev_month_yoy: str | None)
        데이터 부족 또는 계산 실패 시 (None, None)
    """
    rows_s = sorted(rows, key=lambda r: r["TIME"])
    if len(rows_s) < 14:          # rows[-2] ~ rows[-14] 최소 14행
        return None, None
    try:
        def pct(a, b):
            fa, fb = float(a), float(b)
            return round((fa / fb - 1) * 100, 1) if fb != 0 else None

        # 최신 확정치(rows[-2]) vs 전년동월(rows[-14])
        cur  = pct(rows_s[-2]["DATA_VALUE"], rows_s[-14]["DATA_VALUE"])
        prev = (
            pct(rows_s[-3]["DATA_VALUE"], rows_s[-15]["DATA_VALUE"])
            if len(rows_s) >= 15 else None
        )
        return (
            str(cur)  if cur  is not None else None,
            str(prev) if prev is not None else None,
        )
    except (ValueError, ZeroDivisionError, IndexError, KeyError):
        return None, None


# ─────────────────────────────────────────────────────────────
# 5. 트렌드 계산 헬퍼
# ─────────────────────────────────────────────────────────────
def _calc_trend(v: str, p: str) -> str:
    """value > prev_value → ▲, < → ▼, = → →. 변환 실패 시 → 반환."""
    try:
        fv = float(str(v).replace(",", "").replace("+", ""))
        fp = float(str(p).replace(",", "").replace("+", ""))
        return "▲" if fv > fp else ("▼" if fv < fp else "→")
    except Exception:
        return "→"


# ─────────────────────────────────────────────────────────────
# 5a. TIME 중복 제거 헬퍼 (기간말/기간평균 2행 반환 지표용)
# ─────────────────────────────────────────────────────────────
def _dedup_by_time(rows: list) -> list:
    """
    같은 TIME에 여러 행이 있을 때 ITEM_CODE2 기준 마지막 행을 유지한다.
    731Y004 등 기간말(0000100)·기간평균(0000200)을 모두 반환하는 지표에 사용.
    TIME·ITEM_CODE2 오름차순 정렬 → 같은 TIME의 마지막 항목(기간평균=0000200) 보존.
    """
    sorted_rows = sorted(
        rows,
        key=lambda r: (r.get("TIME", ""), r.get("ITEM_CODE2", "")),
    )
    seen: dict = {}
    for row in sorted_rows:
        seen[row.get("TIME", "")] = row
    return sorted(seen.values(), key=lambda r: r.get("TIME", ""))


# ─────────────────────────────────────────────────────────────
# 6. 환율 처리 (일별)
# ─────────────────────────────────────────────────────────────
def _fetch_fx(api_key: str, spec: dict) -> Optional[dict]:
    """일별 환율 API 호출 → {value, prev_value, as_of} 반환. 실패 시 None."""
    rows = _fetch_rows(api_key, spec["stat_code"], spec["item_code"], "D")
    if not rows:
        return None
    rows_s = sorted(rows, key=lambda r: r["TIME"])
    try:
        latest = rows_s[-1]
        prev   = rows_s[-2] if len(rows_s) >= 2 else None
        val    = str(round(float(latest["DATA_VALUE"])))
        pval   = str(round(float(prev["DATA_VALUE"]))) if prev else None
        t      = latest["TIME"]   # YYYYMMDD
        as_of  = f"{t[:4]}-{t[4:6]}-{t[6:]}"
        return {"value": val, "prev_value": pval, "as_of": as_of}
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 7. 월별 YoY 처리 (CPI, 수출 공통)
# ─────────────────────────────────────────────────────────────
def _fetch_monthly_yoy(api_key: str, spec: dict) -> Optional[dict]:
    """월별 지표 YoY 계산 → {value, prev_value, as_of} 반환. 실패 시 None."""
    rows = _fetch_rows(api_key, spec["stat_code"], spec["item_code"], "M")
    if not rows:
        return None
    rows_s = sorted(rows, key=lambda r: r["TIME"])
    yoy, prev_yoy = _yoy(rows_s)
    if yoy is None:
        return None
    # as_of = rows[-2] 기준 (최신 확정치, _yoy() 와 동일 기준)
    t     = rows_s[-2]["TIME"]   # YYYYMM
    as_of = f"{t[:4]}-{t[4:]}"
    return {"value": yoy, "prev_value": prev_yoy, "as_of": as_of}


# ─────────────────────────────────────────────────────────────
# 8. 월별 절댓값 처리 (기준금리 등 — YoY 아님)
# ─────────────────────────────────────────────────────────────
def _fetch_base_rate(api_key: str, spec: dict) -> Optional[dict]:
    """
    월별/분기별 지표 최신 절댓값 → {value, prev_value, as_of} 반환. 실패 시 None.
    기준금리처럼 YoY가 아닌 실제 수치를 그대로 사용하는 지표에 사용.
    spec['scale'] 이 있으면 값에 곱한다 (예: 백만달러→억달러 시 0.01).
    """
    period = spec.get("period", "M")
    rows = _fetch_rows(api_key, spec["stat_code"], spec["item_code"], period)
    if not rows:
        return None
    rows_s = sorted(rows, key=lambda r: r["TIME"])
    # S2-3: dedup=True 지표는 같은 TIME의 중복 행 제거 (예: 731Y004 기간말/기간평균)
    if spec.get("dedup"):
        rows_s = _dedup_by_time(rows_s)
    try:
        scale  = spec.get("scale", 1.0)
        latest = rows_s[-1]
        prev   = rows_s[-2] if len(rows_s) >= 2 else None
        val    = str(round(float(latest["DATA_VALUE"]) * scale, 1))
        pval   = str(round(float(prev["DATA_VALUE"]) * scale, 1)) if prev else None
        t      = latest["TIME"]   # YYYYMM or YYYYQn
        if "Q" in t:
            as_of = t  # 분기: "2025Q4" 그대로
        else:
            as_of = f"{t[:4]}-{t[4:]}"
        return {"value": val, "prev_value": pval, "as_of": as_of}
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 9. 자동 코멘트 생성
# ─────────────────────────────────────────────────────────────
def _auto_note(label: str, value: str, prev_value: Optional[str],
               yoy: bool = True) -> str:
    """전/현 값 비교로 간단한 트렌드 코멘트를 생성한다.

    Args:
        label:      지표명 (환율·기준금리 등 특수 케이스 감지에 사용)
        value:      현재 값 (str)
        prev_value: 이전 값 (str)
        yoy:        True → 전년동월 대비, False → 전월/전일 대비
    """
    if not prev_value:
        return "최신 ECOS 데이터 기준"
    try:
        v, p = float(value), float(prev_value)
        d    = v - p
        up   = "상승" if d > 0 else ("하락" if d < 0 else "보합")
        if "환율" in label:
            if "100엔" in label:
                # 원/100엔은 월별 지표 → "전월 대비"
                return f"전월 대비 {abs(d):.1f}원 {up}"
            return f"전일 대비 {abs(d):.0f}원 {up}"
        if "기준금리" in label:
            return f"전월 대비 {abs(d):.2f}%p {up}" if d != 0 else "전월 대비 동결"
        # 보합(d==0) — 환율·기준금리 외 지표
        if d == 0:
            return "전월 대비 동결"
        # YoY 지표 (CPI, 수출증가율 등) → "전년동월 대비"
        if yoy:
            return f"전년동월 대비 {abs(d):.1f}%p {up}"
        return f"전월 대비 {abs(d):.1f}%p {up}"
    except Exception:
        return "최신 ECOS 데이터 기준"


# ─────────────────────────────────────────────────────────────
# 9a. 복합 거시지표 서사 문장 생성
# ─────────────────────────────────────────────────────────────
def _macro_narrative(macro: dict) -> str:
    """
    거시지표 조합 기반 1줄 복합 해석 문장 자동 생성.
    app.py에서 import하여 대시보드 상단에 표시한다.

    Args:
        macro: macro.json 딕셔너리 (_MACRO)

    Returns:
        str — 복합 해석 문장 1줄. 데이터 없으면 빈 문자열.
    """
    from core.utils import safe_float
    fx = safe_float(macro.get("환율(원/$)", {}).get("value", 0))
    exp = safe_float(macro.get("수출증가율", {}).get("value", 0))
    cpi = safe_float(macro.get("소비자물가(CPI)", {}).get("value", 0))
    rate = safe_float(macro.get("기준금리", {}).get("value", 0))
    if fx == 0 and exp == 0 and cpi == 0 and rate == 0:
        return ""

    # 복합 조건 우선순위 순서로 판단
    if fx >= 1500:
        return "환율 1500원대 — 수입 원가 급등 압력, 수출 채산성은 개선되나 내수 부담 확대"
    if fx >= 1450 and exp < 0:
        return "고환율 속 수출 부진 — 가격 경쟁력보다 수요 둔화 영향이 우세한 국면"
    if fx >= 1450 and exp >= 0:
        return "원화 약세가 수출 채산성을 지지하나, 수입 물가 상승 압력도 병존"
    if exp <= -10:
        return "수출 급감 — 글로벌 수요 위축 또는 주요국 규제 리스크 점검 필요"
    if exp < 0 and cpi >= 3.0:
        return "수출 감소 + 고물가 병존 — 스태그플레이션 리스크 모니터링 필요"
    if cpi >= 3.0 and rate < 3.0:
        return "물가 3% 초과 — 한국은행 금리 인하 시기 지연 가능성 높음"
    if cpi < 2.0 and rate >= 3.5:
        return "물가 안정 + 고금리 유지 — 금리 인하 기대감이 형성되는 국면"
    if exp >= 10 and fx < 1380:
        return "수출 호조 + 원화 강세 — 수출 물량 확대에도 채산성 점검 필요"
    if exp >= 5:
        return "수출 회복세 — 반도체 중심 수출 증가, 대외 불확실성은 잔존"
    return "거시지표는 완만한 회복 흐름을 시사하나 불확실성은 잔존"


# ─────────────────────────────────────────────────────────────
# 10. macro.json 원자적 저장
# ─────────────────────────────────────────────────────────────
def _write_macro_atomic(data: dict) -> None:
    """
    macro.json을 원자적으로 저장한다.
    tmp 파일 → fsync → os.replace 순서로 파일 손상을 방지한다.
    """
    MACRO_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=MACRO_PATH.parent,
        prefix=".macro_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, MACRO_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────
# 11. 메인: macro.json 갱신
# ─────────────────────────────────────────────────────────────
def refresh_macro(api_key: Optional[str] = None) -> dict:
    """
    ECOS API에서 거시지표를 수집해 macro.json을 갱신한 뒤 최신 dict를 반환한다.

    Backward Compatible 보장:
      - API 키 없음 → 기존 macro.json 그대로 반환
      - 개별 지표 API 실패 → 해당 지표만 기존 값 유지
      - macro.json 없어도 정상 동작 (신규 생성)

    macro.json 구조:
      - "_meta" 키: 갱신 시각·API 버전 (렌더링 시 skip 필요)
      - 각 지표 entry에 "trend" 키 포함 (▲/▼/→)

    Args:
        api_key: ECOS API 키. None이면 환경변수/secrets에서 자동 조회.

    Returns:
        갱신된 macro dict ({label: {value, prev_value, trend, unit, ...}})
    """
    # ── 기존 파일 로드 (API 실패 시 fallback) ─────────────────
    existing: dict = {}
    if MACRO_PATH.exists():
        try:
            with open(MACRO_PATH, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    # ── API 키 확인 ───────────────────────────────────────────
    key = api_key or _get_api_key()
    if not key:
        print("[ECOS] API 키 없음 → 기존 macro.json 유지")
        return existing

    # ── 각 지표 수집 ──────────────────────────────────────────
    updated: dict = {}
    for label, spec in _SPEC.items():
        old   = existing.get(label, {})
        entry = {
            "unit":        spec["unit"],
            "source_name": spec["source_name"],
            "source_url":  spec["source_url"],
            "frequency":   spec["frequency"],
            "note":        old.get("note", "최신 ECOS 데이터 기준"),
        }

        # fetch_fn 선택: 일별→환율, 월별YoY→CPI/수출, 월별절댓값→기준금리
        if spec["period"] == "D":
            result = _fetch_fx(key, spec)
        elif spec.get("yoy", True):
            result = _fetch_monthly_yoy(key, spec)
        else:
            result = _fetch_base_rate(key, spec)

        if result:
            entry.update(result)
            # trend 계산 (value/prev_value 둘 다 있을 때)
            entry["trend"] = (
                _calc_trend(result["value"], result["prev_value"])
                if result.get("prev_value") else "→"
            )
            if result.get("prev_value"):
                entry["note"] = _auto_note(
                    label, result["value"], result["prev_value"],
                    yoy=spec.get("yoy", True),  # yoy=True → "전년동월 대비"
                )
            print(f"[ECOS] ✓ {label}: {result['value']} {spec['unit']}  trend={entry['trend']}")
        else:
            # 실패 시 기존 값 유지 (trend 포함)
            entry.update({
                "value":      old.get("value",      "N/A"),
                "prev_value": old.get("prev_value", ""),
                "as_of":      old.get("as_of",      ""),
                "note":       old.get("note",       ""),
                "trend":      old.get("trend",      "→"),
            })
            print(f"[ECOS] ✗ {label}: API 실패 → 기존 값 유지")

        updated[label] = entry

    # ── _meta: 갱신 시각·API 버전 기록 ───────────────────────
    updated["_meta"] = {
        "refreshed_at": datetime.now().isoformat(),
        "api_version":  "ECOS StatisticSearch v1",
    }

    # ── macro.json 원자적 저장 ────────────────────────────────
    _write_macro_atomic(updated)
    print(f"[ECOS] macro.json 갱신 완료 → {MACRO_PATH}")

    # ── Phase 13: 갱신 완료 훅 (이상치 탐지) ──────────────────
    try:
        from core.auto_pipeline import on_refresh_complete
        on_refresh_complete(updated)
    except Exception:
        pass

    return updated


# ─────────────────────────────────────────────────────────────
# CLI 실행: python -m core.ecos
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    result = refresh_macro()
    print(json.dumps(result, ensure_ascii=False, indent=2))
