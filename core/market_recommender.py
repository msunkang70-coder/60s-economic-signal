"""
core/market_recommender.py
글로벌 유망 수출시장 추천 엔진.

UN Comtrade 무역 데이터 + 거시지표 + FTA 체결 현황을 종합하여
산업별 상위 3개 유망 수출 시장을 추천한다.
"""

import json
import os
from typing import Any

from core.industry_config import get_profile

# ── 산업별 HS 코드 매핑 ──────────────────────────────────────────
_INDUSTRY_HS: dict[str, str] = {
    "반도체": "8542",
    "자동차": "8703",
    "배터리": "8507",
    "화학":   "2902",
    "소비재": "3304",
    "조선":   "8901",
    "철강":   "7208",
    "일반":   "TOTAL",
}

# ── FTA 체결국 (ISO-3 코드 + 국가명) ────────────────────────────
FTA_COUNTRIES: dict[str, str] = {
    "842": "미국",
    "156": "중국",
    "704": "베트남",
    "036": "호주",
    "124": "캐나다",
    "356": "인도",
    "152": "칠레",
    "604": "페루",
    "702": "싱가포르",
    "458": "말레이시아",
    "360": "인도네시아",
    "764": "태국",
    "608": "필리핀",
    "826": "영국",
    "276": "독일",
    "528": "네덜란드",
    "056": "벨기에",
    "250": "프랑스",
    "380": "이탈리아",
    "616": "폴란드",
    "392": "일본",
}

# ── Mock 데이터 경로 ─────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_MOCK_PATH = os.path.join(_DATA_DIR, "mock_comtrade.json")


def _load_mock_data() -> list[dict]:
    """mock_comtrade.json 로드. 없으면 내장 샘플 반환."""
    if os.path.exists(_MOCK_PATH):
        with open(_MOCK_PATH, encoding="utf-8") as f:
            return json.load(f)
    return _BUILTIN_MOCK


def fetch_comtrade_data(
    hs_code: str,
    reporter: str = "410",
) -> list[dict]:
    """
    UN Comtrade API에서 한국(410) 수출 데이터를 조회한다.

    API 키가 없으면 mock 데이터를 반환한다.

    Args:
        hs_code: HS 코드 (예: "8542")
        reporter: 보고국 코드 (기본 410=한국)

    Returns:
        [{"partner_country": str, "partner_code": str,
          "trade_value_usd": float, "year": int}, ...]
    """
    api_key = os.environ.get("COMTRADE_API_KEY", "")

    if api_key:
        try:
            import requests

            url = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
            params = {
                "reporterCode": reporter,
                "cmdCode": hs_code,
                "flowCode": "X",      # Export
                "period": "recent",
                "motCode": "0",
                "subscription-key": api_key,
            }
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            raw = resp.json()

            results = []
            for row in raw.get("data", []):
                partner = row.get("partnerDesc", "")
                partner_code = str(row.get("partnerCode", ""))
                value = row.get("primaryValue", 0) or 0
                year = row.get("period", 0)
                if partner and partner.lower() != "world":
                    results.append({
                        "partner_country": partner,
                        "partner_code": partner_code,
                        "trade_value_usd": float(value),
                        "year": int(year),
                    })
            return results if results else _load_mock_data()
        except Exception:
            return _load_mock_data()

    return _load_mock_data()


def _calc_cagr(values: list[float]) -> float:
    """최근 3개년 CAGR 계산. 데이터 부족 시 0."""
    if len(values) < 2 or values[0] <= 0:
        return 0.0
    n = len(values) - 1
    ratio = values[-1] / values[0]
    if ratio <= 0:
        return 0.0
    return (ratio ** (1 / n) - 1) * 100


def recommend_markets(
    industry_key: str,
    macro_data: dict[str, Any],
) -> list[dict]:
    """
    산업별 유망 수출 시장 상위 3개국을 추천한다.

    평가 기준 (각 25점, 총 100점):
      a) 수출 성장률 (최근 3년 CAGR)
      b) 시장 규모 (절대값)
      c) 환율 안정성 (macro_data 활용)
      d) 무역 협정 여부 (FTA 체결국)

    Args:
        industry_key: 산업 키
        macro_data: 현재 거시지표 dict

    Returns:
        상위 3개국 리스트
    """
    hs_code = _INDUSTRY_HS.get(industry_key, "TOTAL")
    trade_data = fetch_comtrade_data(hs_code)

    if not trade_data:
        return []

    # 국가별 연도 데이터 그룹핑
    country_years: dict[str, dict[int, float]] = {}
    country_codes: dict[str, str] = {}
    for row in trade_data:
        country = row["partner_country"]
        country_codes[country] = row.get("partner_code", "")
        country_years.setdefault(country, {})[row["year"]] = row["trade_value_usd"]

    # 환율 안정성 점수 (거시지표 기반, 전체 공통)
    fx_stability_score = 15.0  # 기본 중립
    if macro_data:
        fx = macro_data.get("환율(원/$)", {})
        trend = fx.get("trend", "→")
        if trend == "→":
            fx_stability_score = 25.0
        elif trend == "▲":
            fx_stability_score = 18.0  # 원화 약세 → 수출 유리하나 변동성
        else:
            fx_stability_score = 10.0  # 원화 강세 → 수출 불리

    # 국가별 점수 산출
    scored: list[dict] = []
    all_max_values = [
        max(years.values()) for years in country_years.values() if years
    ]
    max_trade = max(all_max_values) if all_max_values else 1.0

    for country, years in country_years.items():
        sorted_years = sorted(years.items())
        values = [v for _, v in sorted_years]
        latest_value = values[-1] if values else 0

        # a) 성장률 (25점)
        cagr = _calc_cagr(values)
        growth_score = min(25.0, max(0.0, (cagr + 10) / 40 * 25))

        # b) 시장 규모 (25점)
        size_score = (latest_value / max_trade * 25) if max_trade > 0 else 0

        # c) 환율 안정성 (25점, 공통)
        stability = fx_stability_score

        # d) FTA 여부 (25점)
        code = country_codes.get(country, "")
        has_fta = code in FTA_COUNTRIES
        fta_score = 25.0 if has_fta else 0.0

        total = round(growth_score + size_score + stability + fta_score)

        # 추천 이유 생성
        reasons = []
        if cagr > 5:
            reasons.append(f"연평균 {cagr:+.1f}% 성장세")
        if size_score > 15:
            reasons.append("대규모 수출 시장")
        if has_fta:
            reasons.append("FTA 체결국 (관세 혜택)")
        if not reasons:
            reasons.append("안정적 교역 파트너")

        scored.append({
            "country": country,
            "country_code": code,
            "score": min(100, total),
            "growth_rate": f"{cagr:+.1f}%",
            "trade_value": _format_usd(latest_value),
            "fta": has_fta,
            "reason": " / ".join(reasons),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:3]


def _format_usd(value: float) -> str:
    """USD 값을 읽기 쉬운 문자열로 변환."""
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:,.0f}"


# ── 내장 Mock 데이터 (API 키 없을 때 사용) ──────────────────────
_BUILTIN_MOCK: list[dict] = [
    # 반도체 (8542) 기준 한국 수출 샘플
    {"partner_country": "중국",     "partner_code": "156", "trade_value_usd": 28_500_000_000, "year": 2023},
    {"partner_country": "중국",     "partner_code": "156", "trade_value_usd": 25_200_000_000, "year": 2022},
    {"partner_country": "중국",     "partner_code": "156", "trade_value_usd": 26_800_000_000, "year": 2021},
    {"partner_country": "베트남",   "partner_code": "704", "trade_value_usd": 18_200_000_000, "year": 2023},
    {"partner_country": "베트남",   "partner_code": "704", "trade_value_usd": 15_800_000_000, "year": 2022},
    {"partner_country": "베트남",   "partner_code": "704", "trade_value_usd": 13_500_000_000, "year": 2021},
    {"partner_country": "미국",     "partner_code": "842", "trade_value_usd": 14_800_000_000, "year": 2023},
    {"partner_country": "미국",     "partner_code": "842", "trade_value_usd": 12_100_000_000, "year": 2022},
    {"partner_country": "미국",     "partner_code": "842", "trade_value_usd": 10_500_000_000, "year": 2021},
    {"partner_country": "일본",     "partner_code": "392", "trade_value_usd": 5_600_000_000,  "year": 2023},
    {"partner_country": "일본",     "partner_code": "392", "trade_value_usd": 5_200_000_000,  "year": 2022},
    {"partner_country": "일본",     "partner_code": "392", "trade_value_usd": 4_900_000_000,  "year": 2021},
    {"partner_country": "인도",     "partner_code": "356", "trade_value_usd": 4_200_000_000,  "year": 2023},
    {"partner_country": "인도",     "partner_code": "356", "trade_value_usd": 3_100_000_000,  "year": 2022},
    {"partner_country": "인도",     "partner_code": "356", "trade_value_usd": 2_300_000_000,  "year": 2021},
    {"partner_country": "독일",     "partner_code": "276", "trade_value_usd": 3_800_000_000,  "year": 2023},
    {"partner_country": "독일",     "partner_code": "276", "trade_value_usd": 3_500_000_000,  "year": 2022},
    {"partner_country": "독일",     "partner_code": "276", "trade_value_usd": 3_200_000_000,  "year": 2021},
    {"partner_country": "싱가포르", "partner_code": "702", "trade_value_usd": 6_500_000_000,  "year": 2023},
    {"partner_country": "싱가포르", "partner_code": "702", "trade_value_usd": 5_800_000_000,  "year": 2022},
    {"partner_country": "싱가포르", "partner_code": "702", "trade_value_usd": 5_100_000_000,  "year": 2021},
    {"partner_country": "멕시코",   "partner_code": "484", "trade_value_usd": 2_100_000_000,  "year": 2023},
    {"partner_country": "멕시코",   "partner_code": "484", "trade_value_usd": 1_800_000_000,  "year": 2022},
    {"partner_country": "멕시코",   "partner_code": "484", "trade_value_usd": 1_500_000_000,  "year": 2021},
]
