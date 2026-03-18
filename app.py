"""
app.py — KDI 나라경제 브라우저
실행: streamlit run app.py
"""

import json
import os
import pathlib   # ADD
import re
import sys
from collections import Counter
from datetime import date as _date, datetime as _dt  # ADD: _dt for timestamp

import pandas as pd  # for trend charts

import streamlit as st

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from core.fetcher import (
    fetch_list   as _fetch_list,
    fetch_detail as _fetch_detail,
)
from core.ecos import refresh_macro as _ecos_refresh, _get_api_key as _ecos_get_key
from core.content_manager import load_content_history as _load_history
from core.industry_config import get_industry_list, get_profile
from core.subcategory_config import get_subcategory_list, get_subcategory_rules, get_subcategory_label
from core.feedback_store import save_feedback
from core.impact_scorer import score_article, score_articles
from core.action_checklist import generate_checklist
from core.analytics import log_event, get_daily_summary
from core.today_signal import generate_today_signal
from core.decision_engine import generate_decision_options
from core.macro_signal_engine import detect_macro_signals, get_signal_summary
from core.industry_mapper import map_industry_impact, get_industry_comparison
from core.strategy_generator import generate_all_insights
from core.impact_logic import (
    calculate_impact_direction,
    get_direction_en,
    get_direction_emoji,
    map_industry_sensitivity,
)
from core.ai_insight_generator import generate_ai_insight

st.set_page_config(
    page_title="MSion | 60s 수출경제신호",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS: mobile responsiveness + Plotly chart spacing ──────────────
st.html("""
<style>
/* ── Research Plan Color Theme ───────────────────── */
/* Page background — very light lavender */
.stApp { background-color: #F4F4FF !important; }
.stMain { background-color: #F4F4FF !important; }

/* Sidebar — light lavender gradient */
[data-testid="stSidebar"] > div:first-child {
    background: linear-gradient(180deg, #EAEBFF 0%, #E0E1FF 100%) !important;
}
[data-testid="stSidebar"] { background-color: #EAEBFF !important; }

/* Tab bar — pill style on lavender bg */
[data-testid="stTabs"] [role="tablist"] {
    background: #EAEBFF;
    border-radius: 12px;
    padding: 4px;
    gap: 4px;
}
[data-testid="stTabs"] [role="tab"] {
    border-radius: 8px !important;
    font-weight: 600 !important;
    color: #5B5FEE !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    background: #5B5FEE !important;
    color: white !important;
}

/* Dividers — lavender tint */
hr { border-color: #C8C9FF !important; opacity: 0.6; }

/* Expander headers */
[data-testid="stExpander"] summary {
    background: #EAEBFF !important;
    border-radius: 8px !important;
}

/* Remove default Streamlit top padding */
.block-container { padding-top: 1.5rem !important; }

/* Mobile: stack columns vertically */
@media (max-width: 768px) {
    [data-testid="column"] { width: 100% !important; min-width: 100% !important; }
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
}

/* Remove plotly chart bottom margin */
.js-plotly-plot { margin-bottom: 0 !important; }

/* Card hover lift effect */
div[data-testid="stMarkdownContainer"] > div:hover {
    transform: translateY(-1px);
    transition: transform 0.15s ease;
}

/* Hide Streamlit hamburger + footer */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent !important; }

/* ── Download Button — Primary CTA 스타일 ─── */
.stDownloadButton > button {
    background: linear-gradient(135deg, #5B5FEE 0%, #7C3AED 100%) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 700 !important;
    font-size: 14px !important;
    padding: 10px 20px !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 2px 8px rgba(91,95,238,0.3) !important;
}
.stDownloadButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 16px rgba(91,95,238,0.45) !important;
    opacity: 0.95 !important;
}
.stDownloadButton > button:active {
    transform: translateY(0px) !important;
}

/* ── 섹션 구분선 — lavender tint ─── */
[data-testid="stHorizontalRule"] hr {
    margin: 24px 0 !important;
    border-top: 1px solid #C8C9FF !important;
    opacity: 0.8 !important;
}

/* ── 섹션 간 상단 여백 통일 ─── */
[data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"] {
    margin-top: 8px !important;
}
</style>
""")

# ── 🔑 Groq API 키를 os.environ 에 주입 ───────────────────────────────────
# @st.cache_data 안에서도 os.environ 은 안정적으로 접근 가능.
# secrets.toml 이 아직 읽히지 않았을 때를 대비한 조기 주입.
try:
    if not os.environ.get("GROQ_API_KEY"):
        _groq_secret = (st.secrets.get("groq") or {}).get("api_key", "").strip()
        if _groq_secret:
            os.environ["GROQ_API_KEY"] = _groq_secret
            print(f"[app] ✅ GROQ_API_KEY 주입 완료 ({len(_groq_secret)}자)")
        else:
            print("[app] ⚠️  GROQ_API_KEY 없음 — 규칙 기반 폴백 사용")
except Exception as _exc:
    print(f"[app] GROQ_API_KEY 주입 오류: {_exc}")

# ── T-09: 이메일에서 기사 앵커 링크 처리 ──────────────────
_query_params = st.query_params
_target_article_id = _query_params.get("article_id", None)

# ── 의미 기반 색상 상수 ─────────────────────────────────
_SEMANTIC_COLORS = {
    "risk":        {"bg": "#fef2f2", "border": "#fca5a5", "text": "#dc2626"},   # 빨강
    "opportunity": {"bg": "#f0fdf4", "border": "#86efac", "text": "#16a34a"},   # 초록
    "watch":       {"bg": "#fefce8", "border": "#fde047", "text": "#ca8a04"},   # 노랑
    "neutral":     {"bg": "#eff6ff", "border": "#93c5fd", "text": "#2563eb"},   # 파랑
}

# ══════════════════════════════════════════════════════
# 1. fetch_list — List 단계 캐시 (TTL 6h)
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_list(url: str, top_n: int) -> list[dict]:
    return _fetch_list(url, top_n)


# ══════════════════════════════════════════════════════
# 2. fetch_detail — Detail 단계 캐시 (TTL 30d)
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=24 * 3600, show_spinner=False)   # TTL 1일 (Gemini 요약 갱신 주기)
def fetch_detail(doc_id: str, url: str, title: str, industry_key: str = "일반") -> dict:
    return _fetch_detail(doc_id, url, title, industry_key=industry_key)



# ══════════════════════════════════════════════════════
# ADD: 거시지표 — JSON 파일 기반 로드 (비용 0원)
# ══════════════════════════════════════════════════════
_MACRO_PATH = pathlib.Path(_BASE) / "data" / "macro.json"  # ADD


def _calc_trend(value: str, prev_value: str) -> str:  # ADD
    """value > prev_value → ▲, < → ▼, = → →"""
    try:
        v = float(str(value).replace(",", "").replace("+", ""))
        p = float(str(prev_value).replace(",", "").replace("+", ""))
        return "▲" if v > p else ("▼" if v < p else "→")
    except (ValueError, TypeError):
        return "→"


def _load_macro() -> dict:  # ADD
    """data/macro.json 로드 + trend 보완 계산.

    '_'로 시작하는 메타 키(_meta 등)는 지표 카드 렌더링에서 제외한다.
    ecos.py가 trend를 저장한 경우 그대로 사용하고,
    없는 경우(수동 작성 macro.json)에는 value/prev_value로 계산한다.
    """
    if _MACRO_PATH.exists():
        with open(_MACRO_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        data = {}
        for k, item in raw.items():
            if k.startswith("_"):       # _meta 등 내부 키 제외
                continue
            # trend 없으면 value/prev_value로 보완 계산
            if "trend" not in item and "prev_value" in item:
                item["trend"] = _calc_trend(item["value"], item["prev_value"])
            data[k] = item
        return data
    return {}


_MACRO: dict = _load_macro()  # ADD: placeholder dict 완전 대체


def _validate_macro_item(label: str, data: dict) -> str | None:
    """
    거시지표 값 범위 검증.
    - 환율(원/$): 1,200 ~ 1,700 정상 범위
    이상이면 경고 메시지 반환, 정상이면 None.
    """
    try:
        val = float(str(data.get("value", "")).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None
    if label == "환율(원/$)" and not (1_200 <= val <= 1_700):
        return (
            f"⚠️ **{label}** 현재 값({val:,.0f}원/$)이 "
            f"정상 범위(1,200~1,700)를 벗어났습니다. "
            f"`data/macro.json`을 업데이트해 주세요."
        )
    if "100엔" in label and not (700 <= val <= 1_300):
        return (
            f"⚠️ **{label}** 현재 값({val:,.1f}원/100엔)이 "
            f"정상 범위(700~1,300)를 벗어났습니다. "
            f"`data/macro.json`을 업데이트해 주세요."
        )
    return None


# ══════════════════════════════════════════════════════
# 정책 분류 상수
# ══════════════════════════════════════════════════════
_POLICY_TYPES = {
    "지원":     ["지원", "보조", "혜택", "육성", "지원금", "보조금"],
    "규제":     ["규제", "제한", "금지", "강화", "단속", "처벌"],
    "구조개편": ["개편", "구조", "혁신", "개혁", "전환", "재편"],
    "위기대응": ["위기", "대응", "긴급", "안정화", "방어", "보호"],
}
_POLICY_TYPE_COLOR = {
    "지원":     ("#d4edda", "#155724"),
    "규제":     ("#f8d7da", "#721c24"),
    "구조개편": ("#fff3cd", "#856404"),
    "위기대응": ("#ede0ff", "#4a1c8c"),
    "일반정책": ("#e8f4fd", "#1a6fa8"),
}
_ECON_KW = [
    "성장", "금리", "환율", "수출", "수입", "물가", "소비", "투자",
    "고용", "재정", "경기", "부채", "기업", "가계", "산업", "정책", "무역",
]
_RISK_KW = ["위기", "악화", "하락", "부진", "우려", "감소", "침체"]
_OPP_KW  = ["성장", "회복", "개선", "증가", "기회", "확대", "호조"]

# 전략 질문 키워드 추출 시 제외할 범용 단어 (문서 차별화용)
_STOP_WORDS = frozenset({
    "경제", "정책", "이슈", "분석", "나라", "관련", "동향", "현황",
    "방안", "대책", "최근", "주요", "향후", "국내", "한국", "글로벌",
    "시장", "영향", "전망", "대응", "상황", "변화", "부분", "내용",
    "산업", "기업", "우리", "사회", "문제", "결과", "중심", "기반",
})

# ── 콘텐츠 관련성 필터 키워드 ─────────────────────────────
_RELEVANCE_KW: list[str] = [
    "수출", "수입", "무역", "환율", "금리", "물가", "경기", "투자",
    "기업", "산업", "성장", "고용", "재정", "부채", "공급망", "원자재",
    "통상", "관세", "FTA", "글로벌", "달러", "금융", "시장", "경상수지",
    "제조", "중소기업", "스타트업", "벤처", "혁신", "디지털", "반도체",
]
_IRRELEVANT_KW: list[str] = [
    # 로컬/소상공인
    "동네", "로컬", "하이퍼로컬", "동네책방", "당근",
    "지역상권", "골목", "소상공인 창업", "프랜차이즈",
    # 규제 행정 (수출 규제는 제외)
    "규제개혁", "규제정보포털", "규제입증요청", "규제합리화",
    "규제샌드박스", "규제혁신",
    # 인사/연봉/채용
    "직원수", "직원 수", "평균 연봉", "연봉 공개", "채용공고",
    "회장 수령", "임원 보수",
    # 은행/핀테크 장애
    "먹통", "접속 장애", "서버 장애", "대기인원",
    # SNS/엔터
    "카카오뱅크", "카카오페이", "토스", "네이버페이",
    "아이돌", "K-POP", "드라마", "영화 흥행",
    # 부동산
    "아파트 분양", "청약", "전세", "월세",
    # 스포츠
    "프로야구", "프로축구", "올림픽 메달",
    # V17.7: 자본시장/거버넌스/정치 일반
    "투자자 보호", "코스피", "코스닥", "상법 개정", "상법개정",
    "거버넌스", "지배구조 개선", "주주총회", "의결권",
    "SK하이닉스", "삼성전자 주가", "주가 전망",
    "국회 통과", "여야 합의", "정치 일정",
]

# V17.7: 산업별 추가 차단 키워드 — 해당 산업과 무관한 주제 차단
_INDUSTRY_BLOCK_KW: dict[str, list[str]] = {
    "소비재": [
        # 자본시장/증시 논평
        "밸류업", "공매도", "주주환원", "배당 확대", "자사주",
        "기업가치 제고", "주가순자산", "PBR", "PER",
        "증시", "주식시장", "시가총액", "상장",
        # 거버넌스/상법 (소비재 ESG는 별도)
        "이사회 구성", "사외이사", "감사위원",
        # 금융/은행업 전용
        "예금", "대출 금리", "은행 실적", "보험사",
        "여신", "수신", "BIS 비율",
    ],
    "반도체": [
        # 소비재/유통 전용
        "K-뷰티", "K-푸드", "화장품", "식품 수출",
        "유통 채널", "이커머스", "쇼핑",
    ],
}

# ADD: 산업 태그 감지 키워드
_INDUSTRY_TAGS = {
    "통상/수출": ["수출", "무역", "관세", "통상", "FTA", "수입", "교역", "통관"],
    "자원안보":  ["에너지", "원자재", "자원", "부품", "소재", "공급망", "희귀"],
    "금융/시장": ["금리", "채권", "주가", "투자", "자본", "금융", "환율", "밸류"],
    "지역/로컬": ["지역", "지방", "중소", "자영업", "소상공인", "로컬"],
}

# ══════════════════════════════════════════════════════
# S2-1: 지표별 임계값 & 신호등 색상
# ══════════════════════════════════════════════════════
_THRESHOLDS: dict[str, list] = {
    "환율(원/$)": [
        (0,    1380, "normal",  "#ffffff", "정상"),
        (1380, 1450, "caution", "#fffbeb", "주의"),
        (1450, 1500, "warning", "#fff3e0", "경고"),
        (1500, 9999, "danger",  "#ffeaea", "위험"),
    ],
    "수출증가율": [
        (-9999, -10, "danger",  "#ffeaea", "급감"),
        (-10,     0, "caution", "#fffbeb", "감소"),
        (0,      15, "normal",  "#ffffff", "정상"),
        (15,   9999, "caution", "#f0fff4", "급증"),
    ],
    "소비자물가(CPI)": [
        (0,    2.0, "normal",  "#ffffff", "안정"),
        (2.0,  3.0, "caution", "#fffbeb", "주의"),
        (3.0, 9999, "danger",  "#ffeaea", "고물가"),
    ],
    "기준금리": [
        (0,    2.0, "caution", "#fffbeb", "저금리"),
        (2.0,  3.5, "normal",  "#ffffff", "정상"),
        (3.5, 9999, "warning", "#fff3e0", "고금리"),
    ],
    # S2-3 신규 지표
    "원/100엔 환율": [
        (0,     800, "danger",  "#ffeaea", "엔저"),
        (800,   900, "caution", "#fffbeb", "주의"),
        (900,  1100, "normal",  "#ffffff", "정상"),
        (1100, 9999, "caution", "#fffbeb", "엔고"),
    ],
    "수출물가지수": [
        (-9999,  -5, "danger",  "#ffeaea", "급락"),
        (-5,      0, "caution", "#fffbeb", "하락"),
        (0,       5, "normal",  "#ffffff", "안정"),
        (5,    9999, "caution", "#f0fff4", "급등"),
    ],
    "수입물가지수": [
        (-9999,  -5, "caution", "#f0fff4", "급락"),
        (-5,      0, "normal",  "#ffffff", "하락"),
        (0,       5, "caution", "#fffbeb", "상승"),
        (5,    9999, "danger",  "#ffeaea", "급등"),
    ],
}
_STATUS_BADGE: dict[str, tuple] = {
    "normal":  ("#22c55e", "✅"),
    "caution": ("#f59e0b", "⚠️"),
    "warning": ("#f97316", "🔶"),
    "danger":  ("#ef4444", "🔴"),
}


def _get_threshold_status(label: str, value_str: str) -> tuple:
    """(status, bg_color, label_text) 반환. 임계값 미정의 시 normal/white 반환."""
    try:
        v = float(str(value_str).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return "normal", "#ffffff", ""
    for lo, hi, status, bg, lbl in _THRESHOLDS.get(label, []):
        if lo <= v < hi:
            return status, bg, lbl
    return "normal", "#ffffff", ""


# S2-2: 오늘 내 사업 영향 한 줄 해석
def _auto_business_impact(key: str, value: float) -> str:
    """지표·값에 따른 수출 중소기업 시각 한 줄 해석."""
    if "환율" in key:
        if value >= 1450:
            return "수출업 유리 구간 — 달러 수금 시 환전 적기. 단, 원자재 수입 원가 상승 주의"
        elif value <= 1300:
            return "수출 가격경쟁력 약화 — 수출 단가 재검토 필요"
        return "환율 안정 구간 — 정상 운영"
    if "수출" in key:
        if value > 5:
            return "수출 호조 — 생산·재고 확대 검토 시점"
        elif value < 0:
            return "수출 감소세 — 주요 수출 시장 수요 점검 필요"
        return "수출 보합 — 주요 시장 동향 지속 모니터링"
    if "CPI" in key or "물가" in key:
        if value >= 3.0:
            return "고물가 지속 — 원가 상승 반영한 단가 재산정 검토"
        elif value >= 2.0:
            return "물가 상승 추세 — 원자재·운송비 비용 압박 주의"
        return "물가 안정 — 원가 부담 완화 국면"
    if "금리" in key:
        if value >= 3.5:
            return "고금리 — 금융 비용 부담 증가, 운전자본 조달 조건 재검토"
        elif value <= 2.0:
            return "저금리 — 설비 투자·시설 확장 자금 조달 유리"
        return "금리 안정 구간 — 정상 금융 환경"
    # S2-3 신규 지표
    if "100엔" in key:
        if value < 800:
            return "엔저 심화 — 일본 경쟁 제품 가격 우위 강화, 대일 수출 가격경쟁력 약화"
        elif value > 1100:
            return "엔고 — 대일 수출 가격경쟁력 개선, 일본산 부품·원자재 수입 원가 상승"
        return "엔화 안정 구간 — 대일 수출입 정상 환경"
    if "수출물가" in key:
        if value < -5:
            return "수출 단가 급락 — 채산성 악화 우려, 수출 단가 구조 긴급 재검토"
        elif value < 0:
            return "수출 단가 하락 — 마진 압박 진행 중, 원가 절감 및 단가 협상 검토"
        elif value >= 5:
            return "수출 단가 급등 — 채산성 개선 기회, 단 가격 경쟁력 약화 여부 점검"
        return "수출 단가 안정 — 정상 수출 가격 환경"
    if "수입물가" in key:
        if value >= 5:
            return "수입 원가 급등 — 생산 원가 상승 압박 심화, 단가 전가 가능 여부 점검"
        elif value >= 0:
            return "수입 원가 상승 — 원자재·부품 조달 비용 증가, 비용 모니터링 강화"
        return "수입 원가 하락 — 원자재·부품 조달 비용 완화, 원가 경쟁력 개선 기회"
    return ""


# ── 지표별 숫자 표기 통일 함수 ────────────────────────────────
_FMT_CURRENCY = frozenset({"환율(원/$)", "원/100엔 환율"})   # 천 단위 콤마, 소수점 2자리
_FMT_PCT_2    = frozenset({                                  # % 소수점 2자리
    "소비자물가(CPI)", "수출증가율", "기준금리",
    "수출물가지수", "수입물가지수",
})


def _fmt_value(label: str, value_raw) -> str:
    """
    지표 레이블에 따라 숫자 표기 규칙을 적용한 문자열 반환.

    환율류 → 천 단위 콤마 + 소수점 2자리  예) 1,476.00
    %류    → 소수점 2자리                  예) 14.80
    기타    → 원본 유지
    """
    try:
        val = float(str(value_raw).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return str(value_raw)

    if label in _FMT_CURRENCY:
        return f"{val:,.2f}"          # 1,476.00
    if label in _FMT_PCT_2:
        return f"{val:.2f}"           # 14.80
    return str(value_raw)             # 기타: 원본 유지


# ADD: 산업 태그 + 정책 성격별 전략 질문 템플릿
_STRATEGY_TEMPLATES: dict[str, list[str]] = {
    "통상/수출": [
        "주요 수출 시장의 집중도 리스크 — 시장 다변화 전략은 준비됐는가?",
        "{kw} 변화가 수출 마진과 환율 헷징 전략에 미치는 영향은?",
        "공급망 충격 대비 대체 조달처 확보 계획은 마련돼 있는가?",
    ],
    "자원안보": [
        "핵심 원자재·부품의 특정국 의존도는 얼마나 되는가?",
        "{kw} 관련 대체 조달 경로와 비용 전가 가능성은?",
        "자원 가격 변동이 생산 원가·마진에 미치는 파급 효과는?",
    ],
    "금융/시장": [
        "{kw} 변화가 자본 조달 비용과 투자 심리에 미치는 영향은?",
        "현재 밸류에이션 수준에서 리스크 프리미엄 재평가가 필요한가?",
        "유동성 리스크 관리와 헷징 포지션은 충분히 준비됐는가?",
    ],
    "지역/로컬": [
        "해당 정책이 지역 유통 채널과 운영 모델에 미치는 영향은?",
        "{kw} 변화에 대응하는 파트너·채널 전략 조정이 필요한가?",
        "로컬 시장 내 경쟁 구도가 정책으로 인해 재편될 가능성은?",
    ],
    "지원": [
        "{kw} 지원 정책의 실질 수혜 대상과 신청 요건은 무엇인가?",
        "지원 정책이 업종 내 경쟁 환경을 변화시킬 가능성은?",
        "정책 일몰 이후 자립 가능성과 출구 전략은 준비됐는가?",
    ],
    "규제": [
        "{kw} 규제 강화가 우리 사업 모델의 어느 부분을 위협하는가?",
        "규제 준수 비용과 타임라인을 이미 예산에 반영했는가?",
        "규제 환경 변화로 신규 진입 장벽이 높아지는가, 낮아지는가?",
    ],
    "구조개편": [
        "{kw} 구조 개편으로 시장 내 경쟁 질서가 어떻게 재편되는가?",
        "구조 변화에 선제적으로 대응하는 포지셔닝 전략은 무엇인가?",
        "장기적 산업 구조 변화 속 핵심 역량을 어떻게 재정의할 것인가?",
    ],
    "위기대응": [
        "{kw} 위기 상황에서 유동성 버퍼와 비상 계획은 충분한가?",
        "위기 대응 정책의 수혜 타임라인과 실질 효과는?",
        "위기 장기화 시 사업 모델의 내구성을 어떻게 확보할 것인가?",
    ],
    "default": [
        "{kw} 정책이 우리 산업에 미치는 2차·3차 파급 효과는?",
        "이 정책 기조가 6개월 이상 지속된다면 시장은 어떻게 재편되는가?",
        "대응 전략과 리스크 헷징 계획은 충분히 준비됐는가?",
    ],
}


# ══════════════════════════════════════════════════════
# 정책 분석 헬퍼
# ══════════════════════════════════════════════════════
def _classify_policy_type(text: str) -> str:
    for ptype, kws in _POLICY_TYPES.items():
        if any(k in text for k in kws):
            return ptype
    return "일반정책"


def _policy_intensity(docs: list) -> int:
    neg_kw = ["위기", "긴급", "폭락", "급등", "충격", "경고", "급격"]
    n = sum(1 for d in docs for k in neg_kw if k in d.get("title", ""))
    return min(5, max(1, n + 2))


def _top_keywords(docs: list, n: int = 5) -> list:
    all_text = " ".join(d["title"] for d in docs)
    words = re.findall(r"[가-힣]{2,}", all_text)
    c = Counter(w for w in words if w in _ECON_KW)
    return [w for w, _ in c.most_common(n)] or ["경제", "정책", "산업"][:n]


def _impact_terms(text: str) -> dict:
    short = "단기 영향 파악 필요"
    mid   = "중기 시장 반응 모니터링 권장"
    long  = "구조적 변화 가능성 검토"
    if any(k in text for k in ["즉시", "단기", "올해", "분기", "당장"]):
        short = "즉각 시행, 단기 영향 명확"
    if any(k in text for k in ["중기", "내년", "점진", "단계적"]):
        mid = "중기 구조 변화 예상"
    if any(k in text for k in ["구조적", "장기", "근본", "5년", "10년"]):
        long = "장기 구조 개편 가능성 높음"
    return {"단기": short, "중기": mid, "장기": long}


def _risk_opportunity(text: str) -> tuple:
    risk_found = [w for w in _RISK_KW if w in text]
    opp_found  = [w for w in _OPP_KW  if w in text]

    if risk_found:
        risk = f"{'·'.join(risk_found[:2])} 관련 부정적 흐름 감지 — 선제적 리스크 점검 권장"
    else:
        # 거시지표 기반 기본 리스크 메시지
        fx = _MACRO.get("환율(원/$)", {})
        try:
            fx_val = float(str(fx.get("value", "0")).replace(",", ""))
        except Exception:
            fx_val = 0
        if fx_val >= 1450:
            risk = "고환율 지속 — 원자재 수입 원가 상승 압박 점검 필요"
        elif fx_val <= 1300:
            risk = "저환율 — 수출 가격경쟁력 약화 모니터링 필요"
        else:
            risk = "현재 단기 리스크 신호 낮음 — 글로벌 공급망 변동 지속 주시"

    if opp_found:
        opp = f"{'·'.join(opp_found[:2])} 관련 긍정적 신호 — 시장 확대 기회 검토"
    else:
        # 거시지표 기반 기본 기회 메시지
        export = _MACRO.get("수출증가율", {})
        try:
            ex_val = float(str(export.get("value", "0")).replace("+", ""))
        except Exception:
            ex_val = 0
        if ex_val > 5:
            opp = f"수출 +{ex_val}% 증가세 — 주요 수출 시장 확대 전략 검토 적기"
        else:
            opp = "거시 안정 구간 — 중장기 시장 다변화 및 신규 바이어 발굴 검토"

    return risk, opp


def _detect_industry_tag(text: str) -> str | None:  # ADD
    for tag, kws in _INDUSTRY_TAGS.items():
        if any(k in text for k in kws):
            return tag
    return None


def build_strategy_questions(doc: dict, detail: dict | None = None, industry_key: str = "일반") -> list:
    """선택 문서 기반 3개 전략 질문 생성 — 산업프로필·정책성격·산업태그·제목 키워드 반영.

    우선순위:
      1. industry_config.strategy_templates (산업 프로필에 정의된 템플릿)
      2. 기존 로직 (산업태그 > 정책성격 > default)
    """
    # 산업 프로필 전략 템플릿이 있으면 우선 사용
    profile = get_profile(industry_key)
    ind_templates = profile.get("strategy_templates", [])

    title     = doc.get("title", "")
    full_text = title

    if detail:
        _s3 = detail.get("summary_3lines", "")
        if isinstance(_s3, dict):
            _s3 = " ".join(str(v) for v in _s3.values() if v)
        full_text += " " + _s3
        full_text += " " + " ".join(detail.get("keywords", []))

    # ── 제목 특정 키워드 추출 (_STOP_WORDS 제외) ──────────────
    title_words = [
        w for w in re.findall(r"[가-힣]{2,}", title)
        if w not in _STOP_WORDS
    ]
    if detail and detail.get("keywords"):
        detail_kws = [k for k in detail["keywords"] if k not in _STOP_WORDS]
        candidates = detail_kws + title_words
    else:
        candidates = title_words

    c  = Counter(candidates)
    ptype = _classify_policy_type(full_text)
    kw = next((w for w, _ in c.most_common(10) if len(w) >= 2), ptype)

    if ind_templates:
        return [t.format(kw=kw) for t in ind_templates[:3]]

    # ── fallback: 산업태그 > 정책성격 > default ───────────────
    industry = _detect_industry_tag(full_text)
    template_key = (
        industry if (industry and industry in _STRATEGY_TEMPLATES)
        else ptype if ptype in _STRATEGY_TEMPLATES
        else "default"
    )
    return [t.format(kw=kw) for t in _STRATEGY_TEMPLATES[template_key][:3]]


# ══════════════════════════════════════════════════════
# UI 블록 렌더 함수
# ══════════════════════════════════════════════════════
def _filter_relevant_docs(
    docs: list, industry_key: str = "일반", subcategory: str = "전체",
) -> tuple[list, list]:
    """관련성 높은 문서와 낮은 문서를 분리 반환. 산업+서브카테고리 키워드로 관련 기사 우선 정렬.

    V17.7: 산업별 차단 키워드(_INDUSTRY_BLOCK_KW) 적용.
    V17.8: 서브카테고리 2-Tier 필터 — include/block/boost 규칙 적용.
           subcategory="전체"이면 기존 로직과 100% 동일 (backward compatible).
    """
    # (1) 기존 산업 필터 — 산업별 추가 차단 키워드
    _ind_block = _INDUSTRY_BLOCK_KW.get(industry_key, [])

    # (2) 서브카테고리 블록 합산 (전체면 빈 리스트 → 기존 동작 유지)
    _sub_rules = get_subcategory_rules(industry_key, subcategory)
    _sub_block = _sub_rules.get("block", [])
    _combined_block = _ind_block + _sub_block if _sub_block else _ind_block

    # (3) include 키워드 (전체면 빈 리스트 → _sub_score 미생성)
    _sub_include = _sub_rules.get("include", [])

    relevant, others = [], []
    for d in docs:
        title = d.get("title", "")
        has_relevant   = any(kw in title for kw in _RELEVANCE_KW)
        has_irrelevant = any(kw in title for kw in _IRRELEVANT_KW)
        has_ind_block  = bool(_combined_block) and any(kw in title for kw in _combined_block)

        if has_relevant and not has_irrelevant and not has_ind_block:
            relevant.append(d)
        else:
            others.append(d)

        # (4) 서브카테고리 include 매칭 → _sub_score 부여 (doc에 저장)
        if _sub_include:
            d["_sub_score"] = sum(1 for kw in _sub_include if kw in title)
        else:
            d["_sub_score"] = 0  # 전체 → 0 (기존 동작)

    # 산업 + 서브카테고리 키워드로 관련도 정렬
    profile = get_profile(industry_key)
    ind_kws = profile.get("keywords", [])

    def _combined_score(doc):
        title = doc.get("title", "")
        ind_s = sum(1 for kw in ind_kws if kw in title) if ind_kws else 0
        sub_s = doc.get("_sub_score", 0) * 2  # 서브매칭 2배 가중
        return ind_s + sub_s

    relevant.sort(key=_combined_score, reverse=True)

    return relevant, others


# ── 3줄 요약 렌더러 ──────────────────────────────────────────
# 구조: ① [핵심 정책] / ② [주요 내용] / ③ [영향·시사점]
_SUMMARY_STYLE = {
    "①": {"color": "#1e40af", "bg": "#eff6ff", "border": "#3b82f6", "label": "핵심 정책"},
    "②": {"color": "#065f46", "bg": "#f0fdf4", "border": "#22c55e", "label": "주요 내용"},
    "③": {"color": "#7c2d12", "bg": "#fff7ed", "border": "#f97316", "label": "영향·시사점"},
}


_4FRAME_STYLE = {
    "impact":      {"icon": "📊", "color": "#1e40af", "bg": "#eff6ff", "border": "#3b82f6", "label": "Impact(영향)"},
    "risk":        {"icon": "📉", "color": "#991b1b", "bg": "#fef2f2", "border": "#ef4444", "label": "Risk(리스크)"},
    "opportunity": {"icon": "💡", "color": "#065f46", "bg": "#f0fdf4", "border": "#22c55e", "label": "Opportunity(기회)"},
    "action":      {"icon": "✅", "color": "#7c2d12", "bg": "#fff7ed", "border": "#f97316", "label": "Action(즉시 행동)"},
}


def _render_summary_3lines(summary_text, source: str = "") -> None:
    """
    요약을 구조화된 카드 형식으로 렌더링.

    summary_text:
      - dict {"impact","risk","opportunity","action"} → 4-frame 카드
      - str → 기존 3줄 카드 (하위 호환)

    source: "groq" | "rule" | "" — 출처 배지 표시용
    """
    if not summary_text:
        st.info("요약 정보가 없습니다.")
        return

    # 출처 배지
    if source in ("groq", "gemini"):
        badge = ('<span style="background:rgba(251,191,36,0.15);color:#b45309;'
                 'font-size:10px;font-weight:700;padding:1px 7px;border-radius:8px;'
                 'border:1px solid rgba(251,191,36,0.4);margin-left:8px">✦ Groq AI</span>')
    elif source == "rule":
        badge = ('<span style="background:#f1f5f9;color:#64748b;'
                 'font-size:10px;font-weight:600;padding:1px 7px;border-radius:8px;'
                 'border:1px solid #e2e8f0;margin-left:8px">규칙 기반</span>')
    else:
        badge = ""

    # ── 4-frame dict 렌더링 ──────────────────────────────────
    if isinstance(summary_text, dict) and all(k in summary_text for k in ("impact", "risk", "opportunity", "action")):
        cards_html = ""
        for key in ("impact", "risk", "opportunity", "action"):
            s = _4FRAME_STYLE[key]
            body = summary_text[key]
            cards_html += (
                f'<div style="display:flex;align-items:flex-start;gap:10px;'
                f'padding:11px 14px;margin-bottom:8px;'
                f'background:{s["bg"]};border-left:4px solid {s["border"]};'
                f'border-radius:0 8px 8px 0">'
                f'<div style="min-width:80px;font-size:10px;font-weight:800;'
                f'color:{s["color"]};line-height:1.4;padding-top:2px;flex-shrink:0">'
                f'{s["icon"]}<br>{s["label"]}</div>'
                f'<div style="font-size:13px;color:#1e293b;line-height:1.75;'
                f'word-break:keep-all;overflow-wrap:break-word">{body}</div>'
                f'</div>'
            )
        st.markdown(
            f'<div style="margin-top:4px">{badge}{cards_html}</div>',
            unsafe_allow_html=True,
        )
        return

    # ── 기존 str 3줄 요약 렌더링 (하위 호환) ─────────────────
    if isinstance(summary_text, str) and not summary_text.strip():
        st.info("요약 정보가 없습니다.")
        return

    lines = [ln.strip() for ln in summary_text.split("\n") if ln.strip()]

    def _card(num: str, body: str, empty: bool = False) -> str:
        style = _SUMMARY_STYLE[num]
        if empty:
            return (
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'padding:10px 14px;margin-bottom:8px;background:#f8fafc;'
                f'border-left:4px solid #e2e8f0;border-radius:0 8px 8px 0;opacity:0.5">'
                f'<div style="min-width:64px;font-size:10px;font-weight:800;color:#94a3b8;'
                f'line-height:1.4">{num}<br>{style["label"]}</div>'
                f'<div style="font-size:12px;color:#94a3b8">정보 없음</div></div>'
            )
        return (
            f'<div style="display:flex;align-items:flex-start;gap:10px;'
            f'padding:11px 14px;margin-bottom:8px;'
            f'background:{style["bg"]};border-left:4px solid {style["border"]};'
            f'border-radius:0 8px 8px 0">'
            f'<div style="min-width:64px;font-size:10px;font-weight:800;'
            f'color:{style["color"]};line-height:1.4;padding-top:2px;flex-shrink:0">'
            f'{num}<br>{style["label"]}</div>'
            f'<div style="font-size:13px;color:#1e293b;line-height:1.75;'
            f'word-break:keep-all;overflow-wrap:break-word">{body}</div>'
            f'</div>'
        )

    cards_html = ""

    # ── 신형 구조 (① ② ③ 레이블 포함) ──────────────────────
    if any(ln.startswith(("①", "②", "③")) for ln in lines):
        rendered_nums = set()
        for ln in lines:
            num = ln[0] if ln else ""
            if num not in _SUMMARY_STYLE:
                continue
            body = ln[1:].strip()
            if body.startswith("[") and "]" in body:
                body = body[body.index("]") + 1:].strip()
            cards_html += _card(num, body)
            rendered_nums.add(num)
        for num in ["①", "②", "③"]:
            if num not in rendered_nums:
                cards_html += _card(num, "", empty=True)

    # ── 구버전 / plain text fallback ─────────────────────────
    else:
        for i, num in enumerate(["①", "②", "③"]):
            body = lines[i] if i < len(lines) else ""
            cards_html += _card(num, body, empty=not body)

    st.markdown(
        f'<div style="margin-top:4px">{badge}{cards_html}</div>',
        unsafe_allow_html=True,
    )


def _render_policy_summary(docs: list) -> None:
    st.html("<br>")
    with st.container(border=True):
        st.markdown("**📋 이번 달 정책 방향 요약**")
        for i, d in enumerate(docs[:3], 1):
            st.markdown(f"{i}. {d['title'][:42]}")
        st.markdown("")
        # 정책 강도 게이지 제거 → 정책 분류 배지로 교체
        ptype_counter: dict[str, int] = {}
        for d in docs[:10]:
            pt = _classify_policy_type(d.get("title", ""))
            ptype_counter[pt] = ptype_counter.get(pt, 0) + 1
        if ptype_counter:
            dominant = max(ptype_counter, key=ptype_counter.get)
            bg, fg = _POLICY_TYPE_COLOR.get(dominant, ("#e8f4fd", "#1a6fa8"))
            st.html(
                f'이번 달 정책 기조 &nbsp;'
                f'<span style="background:{bg};color:{fg};padding:3px 12px;'
                f'border-radius:12px;font-size:12px;font-weight:700">{dominant}</span>'
                f'<span style="font-size:11px;color:#94a3b8;margin-left:8px">'
                f'({ptype_counter[dominant]}건 / {min(len(docs),10)}건 분석)</span>'
            )
        tags = _top_keywords(docs)
        if tags:
            tag_html = " ".join(
                f'<span style="background:#f0f4ff;color:#3a5fc8;'
                f'padding:2px 10px;border-radius:12px;margin:2px;'
                f'font-size:0.8rem">{t}</span>' for t in tags
            )
            st.html(tag_html)


def _render_policy_detail(doc: dict, detail: dict) -> None:
    _s3 = detail.get("summary_3lines", "")
    if isinstance(_s3, dict):
        _s3 = " ".join(str(v) for v in _s3.values() if v)
    full_text  = doc["title"] + " " + _s3
    ptype      = _classify_policy_type(full_text)
    impact     = _impact_terms(full_text)
    risk, opp  = _risk_opportunity(full_text)
    bg, fg     = _POLICY_TYPE_COLOR.get(ptype, ("#e8f4fd", "#1a6fa8"))
    st.html("<br>")
    with st.container(border=True):
        st.markdown("**🏷️ 정책 분석**")
        st.html(
            f'정책 성격 &nbsp; <span style="background:{bg};color:{fg};'
            f'padding:2px 12px;border-radius:4px;font-size:0.85rem"><b>{ptype}</b></span>'
        )
        st.markdown("")
        st.markdown("**⏱ 영향 시계열**")
        for term, desc in impact.items():
            st.markdown(f"- **{term}**: {desc}")
        st.markdown("**⚡ 리스크 · 기회**")
        st.markdown(f"🔴 리스크: {risk}")
        st.markdown(f"🟢 기회:&nbsp;&nbsp; {opp}")


def _render_strategy_questions(doc: dict, detail: dict | None = None) -> None:
    """5-파트 전략 질문 카드 형식으로 렌더링."""
    from core.strategy_generator import generate_strategic_insight
    _ind = st.session_state.get("selected_industry", "일반")
    qs = build_strategy_questions(doc, detail, industry_key=_ind)

    # 현재 매크로 신호에서 가장 관련성 높은 신호 선택 (카드 맥락용)
    if _MACRO:
        try:
            _all_sigs = detect_macro_signals(_MACRO, _ind)
        except Exception:
            _all_sigs = []
    else:
        _all_sigs = []

    _sig_map = {s["label"]: s for s in _all_sigs}

    st.html("<br>")
    st.markdown("### 🎯 전략적 질문 & 시사점")

    _CARD_BG  = {"green": "#f0fdf4", "yellow": "#fffbeb", "red": "#fef2f2"}
    _CARD_BOR = {"green": "#16a34a", "yellow": "#d97706", "red": "#dc2626"}
    _LABEL_BG = {"green": "#dcfce7", "yellow": "#fef3c7", "red": "#fee2e2"}
    _LABEL_FG = {"green": "#15803d", "yellow": "#92400e", "red": "#991b1b"}

    # ── 질문별 연계 신호 사전 할당 (중복 없이) ────────────────────────
    # 키워드 → 지표명 부분문자열 우선순위 매핑
    _Q_TO_SIG = [
        (["환율", "원화", "달러"],                 "환율"),
        (["수출", "수요", "시장", "수주", "AI", "반도체", "무역"], "수출증가율"),
        (["물가", "비용", "원가", "CPI", "인플레"],  "소비자물가"),
        (["금리", "금융", "투자", "자금"],           "기준금리"),
        (["규제", "통제", "제재"],                  "수출증가율"),  # 규제 → 수출 연계
    ]

    def _pick_signal(question: str, pool: list) -> dict | None:
        """풀에서 질문에 가장 맞는 신호 하나 반환 (사용 후 풀에서 제거)."""
        for q_kws, lbl_part in _Q_TO_SIG:
            if any(kw in question for kw in q_kws):
                for sig in pool:
                    if lbl_part in sig["label"]:
                        pool.remove(sig)
                        return sig
        # fallback: 풀의 첫 번째
        return pool.pop(0) if pool else None

    _sig_pool = list(_all_sigs)  # 복사본 (원본 보존)
    _related_sigs = [_pick_signal(q, _sig_pool) for q in qs]
    # 남은 슬롯은 남은 풀로 채움
    for idx, rs in enumerate(_related_sigs):
        if rs is None and _all_sigs:
            _related_sigs[idx] = _all_sigs[min(idx, len(_all_sigs) - 1)]
    # ─────────────────────────────────────────────────────────────────

    for i, q in enumerate(qs):
        items = generate_checklist(q, doc, _ind)
        checklist_html = "".join(
            f'<li style="margin-bottom:4px">☐ {item}</li>' for item in items
        )

        # 사전 할당된 연계 신호 사용 (루프 밖에서 중복 없이 배정됨)
        related_sig = _related_sigs[i] if i < len(_related_sigs) else None

        color       = related_sig["color"] if related_sig else "yellow"
        emoji       = related_sig["emoji"] if related_sig else "🟡"
        color_label = related_sig["color_label"] if related_sig else "주의"
        sig_text    = related_sig["signal"] if related_sig else ""
        impact_text = related_sig["impact"] if related_sig else ""

        bg     = _CARD_BG[color]
        border = _CARD_BOR[color]
        lbg    = _LABEL_BG[color]
        lfg    = _LABEL_FG[color]

        st.html(f"""
        <div style="background:{bg};border:1px solid {border};border-left:5px solid {border};
                    border-radius:10px;padding:18px 20px;margin-bottom:14px">

          <!-- 헤더: 번호 + 색상 배지 -->
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="background:{border};color:#fff;width:22px;height:22px;
                         border-radius:50%;display:flex;align-items:center;
                         justify-content:center;font-size:11px;font-weight:800;
                         flex-shrink:0">{i+1}</span>
            <span style="background:{lbg};color:{lfg};padding:2px 10px;
                         border-radius:12px;font-size:11px;font-weight:700">
              {emoji} {color_label}
            </span>
          </div>

          <!-- 전략 질문 -->
          <div style="font-size:14px;font-weight:700;color:#1e293b;margin-bottom:12px;
                      line-height:1.5">
            🤔 {q}
          </div>

          <!-- 거시 연계 신호 -->
          {"" if not sig_text else f'''
          <div style="background:rgba(255,255,255,0.7);border-radius:6px;padding:8px 12px;
                      margin-bottom:8px;font-size:12px;color:#475569">
            <span style="font-weight:600;color:{border}">📊 연계 거시 신호</span>&nbsp;
            {sig_text}
          </div>'''}

          <!-- 예상 산업 영향 -->
          {"" if not impact_text else f'''
          <div style="background:rgba(255,255,255,0.7);border-radius:6px;padding:8px 12px;
                      margin-bottom:8px;font-size:12px;color:#475569">
            <span style="font-weight:600;color:{border}">🏭 예상 산업 영향</span>&nbsp;
            {impact_text}
          </div>'''}

          <!-- 실행 체크리스트 -->
          <div style="background:rgba(255,255,255,0.8);border-radius:6px;
                      padding:8px 12px;font-size:12px;color:#374151">
            <div style="font-weight:700;color:{border};margin-bottom:6px">✅ 실행 체크리스트</div>
            <ul style="margin:0;padding-left:16px;line-height:1.8">
              {checklist_html}
            </ul>
          </div>

        </div>
        """)


def _render_article_strategy_questions(doc: dict, industry_key: str) -> None:
    """기사 관련 산업 전략 질문 + ✅ 실행 체크리스트 표시."""
    if industry_key == "일반":
        return
    profile = get_profile(industry_key)
    templates = profile.get("strategy_templates", [])
    keywords = profile.get("keywords", [])
    title = doc.get("title", "")

    matched_kw = [kw for kw in keywords if kw in title]
    if not matched_kw or not templates:
        return

    _nc = _SEMANTIC_COLORS["neutral"]
    st.html(f"""
    <div style="background:{_nc['bg']};border:1px solid {_nc['border']};border-radius:10px;
                padding:14px 16px;margin:8px 0">
      <div style="font-size:11px;font-weight:700;color:{_nc['text']};margin-bottom:8px">
        🎯 이 기사를 바탕으로 검토할 전략 질문
      </div>
    </div>
    """)
    for tmpl in templates[:2]:
        question = tmpl.format(kw=matched_kw[0] if matched_kw else "")
        st.markdown(f"- {question}")
        items = generate_checklist(question, doc, industry_key)
        if items:
            st.markdown("\n".join(f"  - ✅ {item}" for item in items))


def _render_policy_industry_impact(doc: dict, industry_key: str) -> None:
    """정책 기사의 산업별 영향 해석 카드."""
    if industry_key == "일반":
        return
    profile = get_profile(industry_key)
    title = doc.get("title", "")

    keywords = profile.get("keywords", [])
    matched = [kw for kw in keywords if kw in title]
    if not matched:
        return

    _wc = _SEMANTIC_COLORS["watch"]
    st.html(f"""
    <div style="background:{_wc['bg']};border:1px solid {_wc['border']};border-radius:10px;
                padding:14px 16px;margin:8px 0">
      <div style="font-size:11px;font-weight:700;color:{_wc['text']};margin-bottom:6px">
        {profile['icon']} {profile['label']} 영향 분석
      </div>
      <div style="font-size:12px;color:#1e293b">
        이 정책은 <b>{', '.join(matched[:3])}</b> 관련 내용으로, {profile['label']} 산업에 직접 영향이 예상됩니다.
      </div>
    </div>
    """)

def _render_article_strategy_questions(doc: dict, industry_key: str) -> None:
    """기사 관련 산업 전략 질문 + ✅ 실행 체크리스트 표시."""
    if industry_key == "일반":
        return
    profile = get_profile(industry_key)
    templates = profile.get("strategy_templates", [])
    keywords = profile.get("keywords", [])
    title = doc.get("title", "")

    matched_kw = [kw for kw in keywords if kw in title]
    if not matched_kw or not templates:
        return

    _nc = _SEMANTIC_COLORS["neutral"]
    st.html(f"""
    <div style="background:{_nc['bg']};border:1px solid {_nc['border']};border-radius:10px;
                padding:14px 16px;margin:8px 0">
      <div style="font-size:11px;font-weight:700;color:{_nc['text']};margin-bottom:8px">
        🎯 이 기사를 바탕으로 검토할 전략 질문
      </div>
    </div>
    """)
    for tmpl in templates[:2]:
        question = tmpl.format(kw=matched_kw[0] if matched_kw else "")
        st.markdown(f"- {question}")
        items = generate_checklist(question, doc, industry_key)
        if items:
            st.markdown("\n".join(f"  - ✅ {item}" for item in items))


def _render_policy_industry_impact(doc: dict, industry_key: str) -> None:
    """정책 기사의 산업별 영향 해석 카드."""
    if industry_key == "일반":
        return
    profile = get_profile(industry_key)
    title = doc.get("title", "")

    keywords = profile.get("keywords", [])
    matched = [kw for kw in keywords if kw in title]
    if not matched:
        return

    _wc = _SEMANTIC_COLORS["watch"]
    st.html(f"""
    <div style="background:{_wc['bg']};border:1px solid {_wc['border']};border-radius:10px;
                padding:14px 16px;margin:8px 0">
      <div style="font-size:11px;font-weight:700;color:{_wc['text']};margin-bottom:6px">
        {profile['icon']} {profile['label']} 영향 분석
      </div>
      <div style="font-size:12px;color:#1e293b">
        이 정책은 <b>{', '.join(matched[:3])}</b> 관련 내용으로, {profile['label']} 산업에 직접 영향이 예상됩니다.
      </div>
    </div>
    """)


# ══════════════════════════════════════════════════════
# 리포트 생성 함수
# ══════════════════════════════════════════════════════
def _get_macro_data_date() -> str:
    """
    _MACRO 각 지표의 as_of에서 월별(YYYY-MM) 기준일만 추출해 최신 날짜 반환.
    형식: "YYYY년 MM월 (최근 발표 기준)"
    환율 등 일별 지표(YYYY-MM-DD)는 제외.
    """
    if not _MACRO:
        return ""
    monthly: list[str] = []
    for item in _MACRO.values():
        as_of = item.get("as_of", "").strip()
        # YYYY-MM 형식만 매칭 (YYYY-MM-DD 형식 제외)
        m = re.match(r"^(\d{4})-(\d{2})(?:\s|$)", as_of)
        if m:
            monthly.append(f"{m.group(1)}-{m.group(2)}")
    if not monthly:
        return ""
    latest = max(monthly)   # 가장 최신 월
    y, mo = latest.split("-")
    return f"{y}년 {int(mo):02d}월 (최근 발표 기준)"


def generate_report_html(
    docs: list,
    sel_doc: dict | None = None,
    detail: dict | None = None,
) -> str:
    import base64 as _b64
    today      = _date.today().strftime("%Y-%m-%d")
    yyyymm     = docs[0]["issue_yyyymm"] if docs else ""
    issue_disp = (
        f"{yyyymm[:4]}년 {int(yyyymm[4:]):02d}월"
        if len(yyyymm) == 6 else yyyymm
    )
    # S1-3: 실제 ECOS 데이터 기준일 (macro.json as_of 기반, 월별 지표 최신 날짜)
    macro_date_disp = _get_macro_data_date() or issue_disp

    # ── 로고 base64 로드 ─────────────────────────────
    logo_tag = ""
    try:
        logo_path = pathlib.Path(_BASE) / "assets" / "logo.png"
        if not logo_path.exists():
            logo_path = pathlib.Path(_BASE) / "assets" / "logo.svg"
        if logo_path.exists():
            mime = "image/png" if logo_path.suffix == ".png" else "image/svg+xml"
            b64  = _b64.b64encode(logo_path.read_bytes()).decode()
            logo_tag = (
                f'<img src="data:{mime};base64,{b64}" alt="MSion" '
                f'style="height:32px;width:auto;margin-bottom:12px;display:block">'
            )
        else:
            logo_tag = '<div style="font-size:18px;font-weight:900;color:#fff;margin-bottom:12px">MSion</div>'
    except Exception:
        logo_tag = '<div style="font-size:18px;font-weight:900;color:#fff;margin-bottom:12px">MSion</div>'

    dirs_html = "\n".join(
        f'<p class="item">{i}. {d["title"][:45]}</p>'
        for i, d in enumerate(docs[:3], 1)
    )
    # 정책 강도 게이지 제거 → 정책 기조 배지
    _ptype_ctr: dict[str, int] = {}
    for _d in docs[:10]:
        _pt = _classify_policy_type(_d.get("title", ""))
        _ptype_ctr[_pt] = _ptype_ctr.get(_pt, 0) + 1
    if _ptype_ctr:
        _dominant = max(_ptype_ctr, key=_ptype_ctr.get)
        _bg, _fg  = _POLICY_TYPE_COLOR.get(_dominant, ("#e8f4fd", "#1a6fa8"))
        policy_badge_html = (
            f'이번 달 정책 기조 &nbsp;'
            f'<span style="background:{_bg};color:{_fg};padding:3px 12px;'
            f'border-radius:12px;font-size:11px;font-weight:700">{_dominant}</span>'
            f'<span style="font-size:10px;color:#94a3b8;margin-left:8px">'
            f'({_ptype_ctr[_dominant]}건 / {min(len(docs),10)}건 분석)</span>'
        )
    else:
        policy_badge_html = ""
    tags_html = " ".join(
        f'<span class="badge">{t}</span>' for t in _top_keywords(docs)
    )

    signal_html = ""
    if sel_doc and detail:
        _s3 = detail.get("summary_3lines", "")
        if isinstance(_s3, dict):
            _s3 = " ".join(str(v) for v in _s3.values() if v)
        full  = sel_doc["title"] + " " + _s3
        ptype = _classify_policy_type(full)
        impact = _impact_terms(full)
        risk, opp = _risk_opportunity(full)
        kw_badges = " ".join(
            f'<span class="badge">{k}</span>'
            for k in detail.get("keywords", [])[:5]
        )
        impact_rows = "\n".join(
            f'<p class="item"><b>{k}:</b> {v}</p>' for k, v in impact.items()
        )
        signal_html = f"""
  <div class="section">
    <h2>🔍 핵심 정책 신호</h2>
    <p class="item" style="font-weight:600">{sel_doc['title'][:50]}</p>
    <p class="item"><b>정책 성격:</b> {ptype}</p>
    {impact_rows}
    <p class="item">🔴 리스크: {risk}</p>
    <p class="item">🟢 기회:&nbsp;&nbsp; {opp}</p>
    <div style="margin-top:10px">{kw_badges}</div>
  </div>"""

    # ── 거시지표 카드 계층화 (4대 핵심 대형 + 보조 3개 소형) ──
    _PRIMARY_LABELS   = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
    _SECONDARY_LABELS = ["원/100엔 환율", "수출물가지수", "수입물가지수"]
    _STATUS_COLOR_MAP = {
        "normal":  "#22c55e",
        "caution": "#f59e0b",
        "warning": "#f97316",
        "danger":  "#ef4444",
    }

    def _card_html(label: str, d: dict, large: bool = False, is_key_indicator: bool = False) -> str:
        status, _, status_lbl = _get_threshold_status(label, str(d.get("value", "")))
        bar_color = _STATUS_COLOR_MAP.get(status, "#22c55e")
        val_size  = "28px" if large else "20px"
        try:
            val_f  = float(str(d.get("value", "0")).replace(",", "").replace("+", ""))
            from core.signal_interpreter import interpret_signal as _interp_sig
            _interp = _interp_sig(label, val_f, d.get("trend", "→"), st.session_state.get("selected_industry", "일반"))
            impact = f'{_interp["signal"]} — {_interp["impact"]}'
        except Exception:
            impact = ""
        impact_html = (
            f'<div style="font-size:10px;color:#1e40af;background:#eff6ff;'
            f'border-left:2px solid #3b82f6;padding:4px 8px;margin-top:8px;'
            f'border-radius:0 4px 4px 0;line-height:1.5">💡 {impact}</div>'
        ) if impact and large else ""
        key_badge = (
            '<span style="background:#fef3c7;color:#92400e;padding:1px 6px;'
            'border-radius:8px;font-size:9px;font-weight:700;margin-left:4px">'
            '⭐핵심</span>'
        ) if is_key_indicator else ""
        badge = (
            f'{key_badge}'
            f'<span style="background:{bar_color};color:#fff;padding:1px 7px;'
            f'border-radius:8px;font-size:9px;font-weight:700;margin-left:6px">'
            f'{status_lbl}</span>'
        ) if status_lbl else key_badge
        trend_icon = "↑" if d.get("trend") == "▲" else ("↓" if d.get("trend") == "▼" else "→")
        trend_color = "#16a34a" if d.get("trend") == "▲" else ("#dc2626" if d.get("trend") == "▼" else "#94a3b8")
        pad = "18px" if large else "12px"
        return (
            f'<div style="padding:{pad};border:1px solid #e2e8f0;'
            f'border-top:3px solid {bar_color};border-radius:8px;background:#fff">'
            f'<div style="font-size:10px;color:#94a3b8;margin-bottom:4px">'
            f'{label}{badge}</div>'
            f'<div style="font-size:{val_size};font-weight:900;color:#0f172a">'
            f'{_fmt_value(label, d.get("value",""))}'
            f'<span style="font-size:12px;color:#64748b;margin-left:2px">{d.get("unit","")}</span>'
            f'<span style="font-size:14px;color:{trend_color};margin-left:4px">{trend_icon}</span>'
            f'</div>'
            f'<div style="font-size:10px;color:#94a3b8;margin-top:4px">'
            f'{d.get("note","")} | {d.get("as_of","")}</div>'
            f'{impact_html}'
            f'</div>'
        )

    _rpt_ind_key = st.session_state.get("selected_industry", "일반") if hasattr(st, "session_state") else "일반"
    _rpt_mw = get_profile(_rpt_ind_key).get("macro_weights", {})
    primary_cards   = "".join(_card_html(l, _MACRO[l], large=True,  is_key_indicator=_rpt_mw.get(l, 0) >= 1.5)  for l in _PRIMARY_LABELS   if l in _MACRO)
    secondary_cards = "".join(_card_html(l, _MACRO[l], large=False, is_key_indicator=_rpt_mw.get(l, 0) >= 1.5) for l in _SECONDARY_LABELS if l in _MACRO)

    macro_section = f"""
<div class="section">
  <h2>📈 핵심 거시지표</h2>
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px">
    {primary_cards}
  </div>
  <div style="font-size:10px;color:#94a3b8;font-weight:700;
              text-transform:uppercase;letter-spacing:1px;
              margin-bottom:8px;padding-top:8px;border-top:1px solid #f1f5f9">
    보조 지표 — 무역 심층
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">
    {secondary_cards}
  </div>
</div>
"""

    # ADD: 전략 질문 문서별 생성 (산업 키 반영)
    _rpt_ind = st.session_state.get("selected_industry", "일반") if hasattr(st, "session_state") else "일반"
    qs = build_strategy_questions(sel_doc, detail, industry_key=_rpt_ind) if sel_doc else [
        "우리 산업에 미치는 2차·3차 파급 효과는?",
        "이 정책 기조가 6개월 이상 지속된다면 시장은 어떻게 재편되는가?",
        "대응 전략과 리스크 헷징 계획은 충분히 준비됐는가?",
    ]
    qs_html = "\n".join(f'<p class="q">▸ {q}</p>' for q in qs)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>나라경제 정책 리포트 {today}</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
       background:#f5f5f5;margin:0;padding:36px;color:#222}}
  .page{{max-width:820px;margin:0 auto;background:#fff;
         padding:52px;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
  .header{{background:linear-gradient(135deg,#071123 0%,#0f2240 100%);
           padding:28px 36px;border-radius:10px;margin-bottom:28px}}
  .header h1{{font-size:22px;font-weight:900;color:#ffffff;
              margin:0 0 6px;letter-spacing:-0.3px}}
  .meta{{font-size:12px;color:#94a3b8;line-height:2}}
  .meta b{{color:#60a5fa}}
  .section{{margin-bottom:22px;padding:22px 24px;
            border:1px solid #ebebeb;border-radius:8px}}
  .section h2{{font-size:14px;font-weight:700;margin:0 0 14px;
               color:#111;border-left:4px solid #333;padding-left:10px}}
  .item{{font-size:13px;line-height:1.8;margin:3px 0;color:#444}}
  .badge{{display:inline-block;background:#f0f4ff;color:#3a5fc8;
          padding:2px 10px;border-radius:12px;font-size:11px;margin:2px}}
  .intensity{{font-family:monospace;font-size:15px;letter-spacing:2px}}
  .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
  .macro-card{{padding:16px;border:1px solid #ebebeb;border-radius:8px}}
  .macro-label{{font-size:11px;color:#999;margin-bottom:4px}}
  .macro-val{{font-size:22px;font-weight:800}}
  .macro-note{{font-size:11px;color:#666;margin-top:6px;line-height:1.6}}
  .macro-meta{{font-size:10px;color:#aaa;margin-top:6px;line-height:1.8}}
  .macro-meta a{{color:#aaa}}
  .q{{font-size:13px;line-height:2;margin:3px 0;color:#444}}
  .footer{{margin-top:32px;padding-top:14px;border-top:1px solid #eee;
           font-size:11px;color:#bbb}}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    {logo_tag}
    <h1>60s 수출경제신호 — 정책 브리핑 리포트</h1>
    <div class="meta">
      작성일: <b>{today}</b> &nbsp;|&nbsp;
      데이터 기준: <b>{macro_date_disp}</b> &nbsp;|&nbsp;
      출처: <b>KDI 경제정보센터 · 한국은행 ECOS</b>
    </div>
  </div>
  <div class="section">
    <h2>📋 이번 달 정책 방향 요약</h2>
    {dirs_html}
    <p class="item" style="margin-top:12px">{policy_badge_html}</p>
    <div style="margin-top:8px">{tags_html}</div>
  </div>
  {signal_html}
  {macro_section}
  <div class="section">
    <h2>🤔 전략 질문</h2>
    {qs_html}
  </div>
  <div class="footer">
    <span style="color:#0f2240;font-weight:700">MSion</span> &nbsp;|&nbsp;
    본 리포트는 참고 자료로만 활용하십시오. &nbsp;|&nbsp;
    <a href="https://msion.ai" style="color:#3b82f6">msion.ai</a>
  </div>
</div>
</body>
</html>"""


def export_data_json(
    docs: list,
    sel_doc: dict | None = None,
    detail: dict | None = None,
    sort_order: str = "최신순",  # ADD
) -> str:
    seen, records = set(), []
    for d in docs:
        if d["doc_id"] in seen:
            continue
        seen.add(d["doc_id"])
        rec = dict(d)
        if sel_doc and d["doc_id"] == sel_doc.get("doc_id") and detail:
            for k, v in detail.items():
                rec[k] = (v or "")[:300] if k == "body_text" else v
        records.append(rec)

    # ADD: questions + sort_order + macro 포함
    _exp_ind = st.session_state.get("selected_industry", "일반") if hasattr(st, "session_state") else "일반"
    questions = build_strategy_questions(sel_doc, detail, industry_key=_exp_ind) if sel_doc else []
    payload = {
        "exported_at":    _date.today().isoformat(),
        "source":         "KDI 경제정보센터 나라경제",
        "sort_order":     sort_order,       # ADD
        "total_docs":     len(records),
        "macro_snapshot": _MACRO,           # ADD: full macro schema 포함
        "strategy_questions": questions,    # ADD: 문서별 전략 질문
        "records":        records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_download_section(docs: list) -> None:
    sel_doc = st.session_state.get("last_doc")
    detail  = st.session_state.get("last_detail")
    sort_order = st.session_state.get("sort_order", "최신순")  # ADD

    today  = _date.today().strftime("%Y%m%d")
    yyyymm = docs[0]["issue_yyyymm"] if docs else "000000"

    _section_header("⬇️ 다운로드", "브리핑 리포트 저장")
    report_bytes = generate_report_html(docs, sel_doc, detail).encode("utf-8")
    st.download_button(
        label="📋 1페이지 리포트 (HTML) 다운로드",
        data=report_bytes,
        file_name=f"report_{yyyymm}_{today}.html",
        mime="text/html",
        use_container_width=True,
        key="dl_report_html",
    )
    with st.expander("⚙️ 고급 — 원본 데이터 내보내기 (개발자용)"):
        st.caption("전체 기사 데이터를 JSON 형식으로 내보냅니다.")
        export_bytes = export_data_json(docs, sel_doc, detail, sort_order).encode("utf-8")
        st.download_button(
            label="📦 전체 데이터 JSON 다운로드",
            data=export_bytes,
            file_name=f"data_{yyyymm}_{today}.json",
            mime="application/json",
            use_container_width=True,
            key="dl_export_json",
        )


# ══════════════════════════════════════════════════════
# 콘텐츠 이력 렌더러 (content_db.json 기반)
# ══════════════════════════════════════════════════════
def _render_content_history() -> None:
    """
    content_db.json 기반 콘텐츠 생성 이력을 표시한다.

    레이아웃:
      - 날짜별 버튼 목록 (최신순)
      - 클릭 시 하단에 스크립트·SRT 뷰어 + 다운로드 버튼 표시
      - 각 파일의 실제 존재 여부를 확인해 뱃지로 표시

    경로 처리:
      content_db.json의 경로는 프로젝트 루트 기준 상대경로(POSIX)이므로
      _BASE(프로젝트 루트 절대경로)와 결합해 절대경로로 변환한다.
    """
    _project_root = pathlib.Path(_BASE)

    st.markdown("---")
    st.markdown("### 📂 최근 생성된 콘텐츠")

    records = _load_history(limit=20)

    if not records:
        st.info(
            "아직 생성된 콘텐츠가 없습니다. "
            "`main.py`를 실행하면 자동으로 이곳에 기록됩니다."
        )
        return

    st.caption(f"총 {len(records)}건 기록 중 최근 {len(records)}건 표시 | content_db.json")

    # 선택 상태 초기화
    st.session_state.setdefault("history_sel_id", None)

    # ── 콘텐츠 목록 ────────────────────────────────────
    for r in records:
        cid    = r.get("content_id", "")
        dt     = r.get("date", cid)
        topic  = r.get("topic", "경제")

        # 파일 존재 여부 확인
        script_rel = r.get("script_path", "")
        srt_rel    = r.get("srt_path", "")
        script_ok  = bool(script_rel) and (_project_root / script_rel).exists()
        srt_ok     = bool(srt_rel)    and (_project_root / srt_rel).exists()

        # 파일 뱃지 조합
        badges = []
        if script_ok:
            badges.append("📝 스크립트")
        if srt_ok:
            badges.append("🎬 SRT")
        file_status = "  |  ".join(badges) if badges else "⚠️ 파일 없음"

        col_btn, col_meta = st.columns([2, 3])
        with col_btn:
            is_selected = (st.session_state["history_sel_id"] == cid)
            btn_label   = f"{'▶ ' if is_selected else ''}📅 {dt}  [{topic}]"
            if st.button(btn_label, key=f"hist_{cid}", use_container_width=True):
                # 같은 항목 재클릭 → 토글 닫기
                if st.session_state["history_sel_id"] == cid:
                    st.session_state["history_sel_id"] = None
                else:
                    st.session_state["history_sel_id"] = cid
                st.rerun()
        with col_meta:
            gen_at = r.get("generated_at", "")
            st.caption(f"{file_status}   생성: {gen_at}")

    # ── 선택된 콘텐츠 상세 뷰어 ────────────────────────
    sel_id = st.session_state["history_sel_id"]
    if not sel_id:
        return

    sel_rec = next((r for r in records if r.get("content_id") == sel_id), None)
    if not sel_rec:
        return

    st.markdown(f"#### 📄 콘텐츠 상세 — {sel_rec.get('date', sel_id)}")

    # 스크립트 뷰어
    script_rel = sel_rec.get("script_path", "")
    if script_rel:
        script_abs = _project_root / script_rel
        if script_abs.exists():
            with st.expander("📝 60초 스크립트 보기", expanded=True):
                script_text = script_abs.read_text(encoding="utf-8")
                st.text_area(
                    label="",
                    value=script_text,
                    height=300,
                    disabled=True,
                    label_visibility="collapsed",
                    key=f"hist_script_{sel_id}",
                )
                st.download_button(
                    label="⬇️ 스크립트(.txt) 다운로드",
                    data=script_text.encode("utf-8"),
                    file_name=f"script_{sel_id}.txt",
                    mime="text/plain",
                    key=f"dl_script_{sel_id}",
                    use_container_width=True,
                )
        else:
            st.warning(f"스크립트 파일을 찾을 수 없습니다: `{script_rel}`")

    # SRT 뷰어
    srt_rel = sel_rec.get("srt_path", "")
    if srt_rel:
        srt_abs = _project_root / srt_rel
        if srt_abs.exists():
            with st.expander("🎬 SRT 자막 파일 보기", expanded=False):
                srt_text = srt_abs.read_text(encoding="utf-8")
                st.text_area(
                    label="",
                    value=srt_text,
                    height=200,
                    disabled=True,
                    label_visibility="collapsed",
                    key=f"hist_srt_{sel_id}",
                )
                st.download_button(
                    label="⬇️ SRT(.srt) 다운로드",
                    data=srt_text.encode("utf-8"),
                    file_name=f"script_{sel_id}.srt",
                    mime="text/plain",
                    key=f"dl_srt_{sel_id}",
                    use_container_width=True,
                )
        else:
            st.warning(f"SRT 파일을 찾을 수 없습니다: `{srt_rel}`")

    # 메타데이터 원본
    with st.expander("🔧 메타데이터 (JSON 원본)", expanded=False):
        st.json(sel_rec)


# ══════════════════════════════════════════════════════
# DASHBOARD INFOGRAPHIC — NEW UI FUNCTIONS
# ══════════════════════════════════════════════════════

def _load_logo_b64() -> str:
    """assets/ 에서 로고 이미지를 base64로 로드. PNG 우선, 없으면 SVG 사용."""
    import base64
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
    """
    헤더에 표시할 LLM 상태 배지 HTML 반환.
    GROQ_API_KEY 있으면 'Groq AI ✦' 배지, 없으면 '규칙 기반' 배지.
    """
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


def _render_dashboard_header() -> None:
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
            <span style="background:rgba(200,245,208,0.2);color:#C8F5D0;padding:2px 10px;
                         border-radius:20px;font-size:10px;font-weight:700;
                         border:1px solid rgba(200,245,208,0.4)">● LIVE</span>
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


def _render_industry_focus(industry_key: str) -> None:
    """Tab 1 상단 — 현재 선택된 산업 컨텍스트 배너 (item 2).

    Example output:
      Industry Focus
      🔬 반도체·디스플레이
    """
    profile    = get_profile(industry_key)
    icon       = profile.get("icon", "📦")
    label      = profile.get("label", industry_key)
    desc       = profile.get("description", "")
    sensitivity = map_industry_sensitivity(industry_key)

    # 민감도 상위 2개 차원 태그
    _DIM_KO = {
        "fx":            "환율 민감",
        "export_growth": "수출 민감",
        "inflation":     "물가 민감",
        "interest_rate": "금리 민감",
    }
    _SENS_COLOR = {"high": "#dc2626", "medium": "#d97706", "low": "#16a34a"}
    _SENS_BG    = {"high": "#fef2f2", "medium": "#fffbeb", "low": "#f0fdf4"}

    sens_tags = "".join(
        f'<span style="background:{_SENS_BG.get(v,"#f1f5f9")};'
        f'color:{_SENS_COLOR.get(v,"#64748b")};padding:2px 10px;'
        f'border-radius:20px;font-size:10px;font-weight:700;margin-right:6px;'
        f'border:1px solid {_SENS_COLOR.get(v,"#94a3b8")}33">'
        f'{_DIM_KO.get(k,"")}</span>'
        for k, v in list(sensitivity.items())[:4]
        if k in _DIM_KO
    )

    st.html(f"""
    <div style="display:flex;align-items:center;justify-content:space-between;
                background:linear-gradient(135deg,#EAEBFF 0%,#E0E1FF 100%);
                border:1px solid #C8C9FF;border-left:5px solid #5B5FEE;
                border-radius:12px;padding:14px 20px;margin-bottom:16px">

      <!-- 좌: Industry Focus + 산업명 -->
      <div style="display:flex;align-items:center;gap:16px">
        <div>
          <div style="font-size:9px;font-weight:700;color:#5B5FEE;
                      text-transform:uppercase;letter-spacing:2px;margin-bottom:4px">
            Industry Focus
          </div>
          <div style="font-size:18px;font-weight:900;color:#1E1B4B;line-height:1.2">
            {icon} {label}
          </div>
          <div style="font-size:11px;color:#4B4F9A;margin-top:3px">{desc}</div>
        </div>
      </div>

      <!-- 우: 민감도 태그들 -->
      <div style="display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end">
        <div style="font-size:9px;color:#8B8FCF;font-weight:600;
                    text-transform:uppercase;letter-spacing:1px;
                    margin-bottom:4px;width:100%;text-align:right">
          Macro Sensitivity
        </div>
        {sens_tags}
      </div>

    </div>
    """)


def _render_kpi_section() -> None:
    """Large KPI cards (4 primary indicators) with Plotly bullet-bar mini-charts.

    Each card shows:
    - 48px value with trend arrow
    - Plotly horizontal bullet chart (current vs range thresholds)
    - Business impact line
    """
    if not _MACRO:
        st.info("거시지표 데이터 없음 — ECOS API 키 설정 후 업데이트 버튼을 클릭하세요.")
        return

    try:
        import plotly.graph_objects as go  # noqa: PLC0415
        _has_plotly = True
    except ImportError:
        _has_plotly = False

    PRIMARY = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
    items = [(k, _MACRO[k]) for k in PRIMARY if k in _MACRO]

    _TREND_COLOR = {"▲": "#16a34a", "▼": "#dc2626", "→": "#64748b"}
    _TREND_ICON  = {"▲": "↑", "▼": "↓", "→": "→"}
    _STATUS_BAR  = {"normal": "#22c55e", "caution": "#f59e0b",
                    "warning": "#f97316", "danger": "#ef4444"}

    # Bullet bar config: (axis_min, axis_max, danger_hi, warn_hi, caution_hi)
    _BULLET_CFG = {
        "환율(원/$)":      (1200, 1700, 1500, 1450, 1380),
        "소비자물가(CPI)": (0,    6,    3.0,  3.0,  2.0),
        "수출증가율":      (-20,  30,   None, None, 0),
        "기준금리":        (0,    6,    None, 3.5,  2.0),
    }

    cols = st.columns(len(items), gap="small")
    for (label, data), col in zip(items, cols):
        with col:
            trend    = data.get("trend", "→")
            unit     = data.get("unit", "")
            val_str  = _fmt_value(label, data.get("value", ""))
            note     = data.get("note", "")
            as_of    = data.get("as_of", "")

            status, bg_color, status_lbl = _get_threshold_status(label, val_str)
            bar_color   = _STATUS_BAR.get(status, "#22c55e")
            trend_color = _TREND_COLOR.get(trend, "#64748b")
            trend_icon  = _TREND_ICON.get(trend, "→")

            try:
                val_float = float(val_str.replace(",", "").replace("+", ""))
            except (ValueError, TypeError):
                val_float = 0.0
            _kpi_ind = st.session_state.get("selected_industry", "일반")
            try:
                from core.signal_interpreter import interpret_signal as _interp_sig
                _interp = _interp_sig(label, val_float, trend, _kpi_ind)
                impact = f'{_interp["signal"]} — {_interp["impact"]}'
            except Exception:
                impact = ""

            # ── Badge (산업 핵심 지표 + 상태 배지) ──────────────
            _kpi_weights = get_profile(_kpi_ind).get("macro_weights", {})
            _is_key = _kpi_weights.get(label, 0) >= 1.5
            key_badge_html = (
                '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;'
                'border-radius:12px;font-size:10px;font-weight:700;margin-right:4px">'
                '⭐핵심</span>'
            ) if _is_key else ""
            badge_html = (
                f'{key_badge_html}'
                f'<span style="background:{bar_color};color:#fff;padding:2px 10px;'
                f'border-radius:12px;font-size:10px;font-weight:800;letter-spacing:.3px">'
                f'{status_lbl}</span>'
                if status_lbl else key_badge_html
            )
            # ── Impact line ────────────────────────────────────
            impact_html = (
                f'<div style="background:#eff6ff;border-left:3px solid #3b82f6;'
                f'padding:7px 10px;border-radius:0 6px 6px 0;font-size:11px;'
                f'color:#1e40af;line-height:1.5;margin:10px 0 6px">💡 {impact}</div>'
                if impact else ""
            )

            # ── Card HTML ──────────────────────────────────────
            st.html(f"""
            <div style="background:#ffffff;border:1px solid #e2e8f0;
                        border-top:5px solid {bar_color};border-radius:14px;
                        padding:22px 20px 14px;
                        box-shadow:0 2px 12px rgba(0,0,0,.07);min-height:240px">
              <div style="display:flex;justify-content:space-between;
                          align-items:center;margin-bottom:8px">
                <span style="font-size:11px;font-weight:700;color:#64748b;
                             text-transform:uppercase;letter-spacing:.8px">{label}</span>
                {badge_html}
              </div>
              <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">
                <span style="font-size:46px;font-weight:900;color:#0f172a;
                             line-height:1;letter-spacing:-2px">
                  {val_str}
                </span>
                <span style="font-size:15px;color:#64748b;font-weight:600">{unit}</span>
                <span style="font-size:26px;font-weight:800;color:{trend_color};margin-left:2px">
                  {trend_icon}
                </span>
              </div>
              <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">{note}</div>
              {impact_html}
              <div style="font-size:10px;color:#cbd5e1;padding-top:6px;
                          border-top:1px solid #f1f5f9">
                기준일: {as_of} &nbsp;|&nbsp; 한국은행 ECOS
              </div>
            </div>
            """)

            # ── Plotly bullet/progress bar ─────────────────────
            if _has_plotly and label in _BULLET_CFG:
                ax_min, ax_max, *_ = _BULLET_CFG[label]
                cfg_vals = _BULLET_CFG[label]
                ax_min, ax_max = cfg_vals[0], cfg_vals[1]

                # Build background range bars
                range_colors = ["#dcfce7", "#fef9c3", "#ffedd5", "#fee2e2"]
                thresholds   = [t for t in cfg_vals[2:] if t is not None]
                boundaries   = sorted(set([ax_min] + thresholds + [ax_max]))
                range_traces = []
                for i in range(len(boundaries) - 1):
                    ci = min(i, len(range_colors) - 1)
                    range_traces.append(go.Bar(
                        x=[boundaries[i + 1] - boundaries[i]],
                        y=[""],
                        base=[boundaries[i]],
                        marker_color=range_colors[ci],
                        orientation="h",
                        showlegend=False,
                        hoverinfo="skip",
                        width=0.5,
                    ))
                # Current value marker
                range_traces.append(go.Scatter(
                    x=[val_float],
                    y=[""],
                    mode="markers+text",
                    marker={"color": bar_color, "size": 14, "symbol": "diamond",
                            "line": {"color": "#ffffff", "width": 2}},
                    text=[f"  {val_str}"],
                    textfont={"size": 10, "color": "#0f172a"},
                    textposition="middle right",
                    showlegend=False,
                    hoverinfo="skip",
                ))

                fig = go.Figure(data=range_traces)
                fig.update_layout(
                    height=52,
                    margin={"t": 0, "b": 0, "l": 0, "r": 0, "pad": 0},
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    barmode="stack",
                    xaxis={
                        "range": [ax_min, ax_max],
                        "showgrid": False,
                        "showticklabels": False,
                        "zeroline": False,
                    },
                    yaxis={"showticklabels": False, "showgrid": False},
                )
                st.plotly_chart(
                    fig, width="stretch",
                    config={"displayModeBar": False, "staticPlot": True},
                )


def _generate_macro_insights() -> list[str]:
    """Generate contextual insight bullets from macro data."""
    insights = []
    for label, data in _MACRO.items():
        val_str = _fmt_value(label, data.get("value", "0"))
        trend   = data.get("trend", "→")
        try:
            val = float(val_str.replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue
        if "환율" in label and "100엔" not in label:
            if val >= 1450:
                insights.append(f"📌 **환율 {val_str}원** — 수출 채산성 개선 구간, 원자재 수입 비용 동시 상승")
            elif val <= 1300:
                insights.append(f"📌 **환율 {val_str}원** — 수출 가격경쟁력 약화 구간, 단가 재검토 필요")
            else:
                insights.append(f"📌 **환율 {val_str}원** — 안정 구간, 정상 수출입 환경")
        if "CPI" in label or "소비자물가" in label:
            if val >= 3.0:
                insights.append(f"📌 **CPI {val_str}%** — 고물가 지속, 금리 인하 기대 약화 요인")
            elif val <= 2.0:
                insights.append(f"📌 **CPI {val_str}%** — 물가 안정, 기준금리 인하 기대 지지")
            else:
                insights.append(f"📌 **CPI {val_str}%** — 물가 상승 추세, 원가 부담 모니터링 필요")
        if "수출증가율" in label:
            if val > 5:
                insights.append(f"📌 **수출증가율 {val_str}%** — 수출 반등세, 생산·재고 확대 검토 시점")
            elif val < 0:
                insights.append(f"📌 **수출증가율 {val_str}%** — 수출 감소세, 주요 시장 수요 긴급 점검 필요")
        if "기준금리" in label:
            if trend == "▼":
                insights.append(f"📌 **기준금리 {val_str}%** — 인하 사이클 진입, 금융 비용 완화 기대")
            else:
                insights.append(f"📌 **기준금리 {val_str}%** — 동결 기조 유지, 추가 인하 타이밍 주목")
        if "수출물가" in label and val >= 5:
            insights.append(f"📌 **수출물가 +{val_str}%** — 수출 채산성 개선 기회, 단 가격경쟁력 약화 여부 점검")
        if "수입물가" in label and val >= 5:
            insights.append(f"📌 **수입물가 +{val_str}%** — 원자재 조달 원가 상승 압박 심화")
    if not insights:
        insights = [
            "📌 최신 거시지표를 업데이트해 인사이트를 확인하세요",
            "📌 ECOS API 키 설정 후 업데이트 버튼을 클릭하세요",
        ]
    return insights[:5]


def _md_to_html(text: str) -> str:
    """**bold** → <strong style="color:#93c5fd">bold</strong> 변환."""
    return re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#93c5fd">\1</strong>', text)


def _render_macro_overview_and_insights() -> None:
    """Key Insights 다크 카드 — 전체 폭 (Overview 텍스트 표 제거)."""
    insights = _generate_macro_insights()
    ins_html = "".join(
        f'<div style="padding:10px 0;border-bottom:1px solid #1e3a5f;'
        f'font-size:13px;color:#e2e8f0;line-height:1.6">{_md_to_html(ins)}</div>'
        for ins in insights
    )
    st.html(f"""
    <div style="background:#0f172a;border-radius:12px;padding:22px 24px">
      <div style="font-size:11px;font-weight:700;color:#60a5fa;
                  text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">
        🔍 Key Macro Insights — 수출 중소기업 시각
      </div>
      {ins_html}
      <div style="font-size:10px;color:#475569;margin-top:12px">
        한국은행 ECOS API 기반 자동 생성 인사이트
      </div>
    </div>
    """)


def _render_signal_cards(industry_key: str) -> None:
    """Gauge 차트 대신 Signal → Impact → Risk → Action 카드 표시."""
    from core.signal_interpreter import interpret_all_signals
    if not _MACRO:   # 5-A: 데이터 없는 경우 방어
        return
    signals = interpret_all_signals(_MACRO, industry_key)
    if not signals:
        return

    st.html("""
    <div style="margin:8px 0">
      <span style="font-size:11px;font-weight:700;color:#64748b;
                   text-transform:uppercase;letter-spacing:1.5px">
        🎯 산업별 경제 신호 해석
      </span>
    </div>
    """)

    # 상위 4개만 카드로 표시
    top = signals[:4]
    cols = st.columns(len(top), gap="small")

    _COLOR = {"▲": "#dc2626", "▼": "#2563eb", "→": "#6b7280"}
    _rc = _SEMANTIC_COLORS["risk"]
    _oc = _SEMANTIC_COLORS["opportunity"]
    _nc = _SEMANTIC_COLORS["neutral"]

    for sig, col in zip(top, cols):
        with col:
            tc = _COLOR.get(sig["trend"], "#6b7280")
            risk_html = (
                f'<div style="font-size:11px;color:{_rc["text"]};margin-top:6px;'
                f'background:{_rc["bg"]};border:1px solid {_rc["border"]};'
                f'border-radius:6px;padding:3px 7px">⚠️ {sig["risk"]}</div>'
            ) if sig["risk"] and sig["risk"] != "—" else ""
            st.html(f"""
            <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;
                        padding:16px;margin-bottom:4px">
              <div style="font-size:11px;color:#64748b;font-weight:600">{sig['label']}</div>
              <div style="font-size:24px;font-weight:800;color:#0f172a;margin:4px 0">
                {sig['value']}<span style="font-size:14px;color:{tc};margin-left:4px">{sig['trend']}</span>
              </div>
              <div style="font-size:12px;font-weight:600;color:{_nc["text"]};margin:6px 0;
                          padding:4px 8px;background:{_nc["bg"]};border-radius:6px">
                📡 {sig['signal']}
              </div>
              <div style="font-size:11px;color:#334155;margin-top:4px">💼 {sig['impact']}</div>
              {risk_html}
              <div style="font-size:11px;color:{_oc["text"]};margin-top:6px">✅ {sig['action']}</div>
            </div>
            """)


def _render_status_pulse_strip() -> None:
    """Full-width compact status strip — all 4 primary indicators in one bar.

    Shows: Indicator name | value | status badge — quick glance for mobile.
    """
    PRIMARY = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
    _STATUS_COLORS = {
        "normal":  ("#dcfce7", "#166534", "✅"),
        "caution": ("#fef9c3", "#854d0e", "⚠️"),
        "warning": ("#ffedd5", "#9a3412", "🔶"),
        "danger":  ("#fee2e2", "#991b1b", "🔴"),
    }
    items = [(k, _MACRO[k]) for k in PRIMARY if k in _MACRO]
    if not items:
        return

    cells_html = ""
    for label, data in items:
        val_str = _fmt_value(label, data.get("value", ""))
        unit    = data.get("unit", "")
        trend   = data.get("trend", "→")
        status, _, status_lbl = _get_threshold_status(label, val_str)
        bg, fg, icon = _STATUS_COLORS.get(status, ("#f1f5f9", "#1e293b", ""))
        short_label  = label.replace("소비자물가(CPI)", "CPI").replace("환율(원/$)", "환율").replace("기준금리", "금리").replace("수출증가율", "수출YoY")
        cells_html += f"""
        <div style="flex:1;background:{bg};border-radius:8px;padding:10px 14px;
                    margin:0 4px;text-align:center;min-width:100px">
          <div style="font-size:9px;font-weight:700;color:{fg};
                      text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">
            {short_label}
          </div>
          <div style="font-size:18px;font-weight:900;color:#0f172a;line-height:1.1">
            {val_str}<span style="font-size:10px;color:#64748b"> {unit}</span>
          </div>
          <div style="font-size:10px;color:{fg};font-weight:700;margin-top:3px">
            {icon} {status_lbl if status_lbl else "—"} {trend}
          </div>
        </div>
        """

    st.html(f"""
    <div style="display:flex;gap:0;margin-bottom:20px;margin-top:-8px">
      {cells_html}
    </div>
    """)


def _render_secondary_indicators() -> None:
    """Compact row for non-primary indicators (엔화, 수출물가, 수입물가) — softer, dimmer style."""
    SECONDARY = ["원/100엔 환율", "수출물가지수", "수입물가지수"]
    items = [(k, _MACRO[k]) for k in SECONDARY if k in _MACRO]
    if not items:
        return

    st.html("""
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;margin-top:4px">
      <span style="background:#8B8FCF;color:white;min-width:26px;height:26px;
                   border-radius:50%;display:inline-flex;align-items:center;
                   justify-content:center;font-size:12px;font-weight:800;flex-shrink:0">5</span>
      <span style="font-size:13px;font-weight:700;color:#4B4F9A">보조 지표</span>
      <span style="font-size:10px;color:#8B8FCF;font-weight:500;
                   background:#F4F4FF;padding:2px 10px;border-radius:20px;
                   border:1px solid #D4D5FF">
        Supporting Indicators
      </span>
    </div>
    """)

    _TREND_COLOR = {"▲": "#16a34a", "▼": "#dc2626", "→": "#94a3b8"}
    cols = st.columns(len(items), gap="small")
    for (label, data), col in zip(items, cols):
        with col:
            trend   = data.get("trend", "→")
            val_str = _fmt_value(label, data.get("value", ""))
            unit    = data.get("unit", "")
            as_of   = data.get("as_of", "")
            source_name = data.get("source_name", "한국은행 ECOS")
            tc      = _TREND_COLOR.get(trend, "#94a3b8")
            _TI     = {"▲": "↑", "▼": "↓", "→": "→"}
            ti      = _TI.get(trend, "→")

            # Short label map
            _SHORT_SEC = {
                "원/100엔 환율":  "JPY/KRW",
                "수출물가지수":   "Export Price",
                "수입물가지수":   "Import Price",
            }
            short_label = _SHORT_SEC.get(label, label)

            st.html(f"""
            <div style="background:#F8F8FF;border:1px dashed #C8C9FF;
                        border-radius:12px;padding:14px 16px;
                        opacity:0.92">
              <!-- Number (smaller than primary) -->
              <div style="display:flex;align-items:baseline;gap:5px;margin-bottom:3px">
                <span style="font-size:1.6rem;font-weight:800;color:#4B4F9A;
                             line-height:1;letter-spacing:-1px">{val_str}</span>
                <span style="font-size:1rem;font-weight:700;color:{tc}">{ti}</span>
              </div>
              <!-- Label -->
              <div style="font-size:10px;font-weight:700;color:#8B8FCF;
                          text-transform:uppercase;letter-spacing:0.8px;margin-bottom:8px">
                {short_label}
                <span style="font-size:9px;color:#A0A3C0;font-weight:400;margin-left:4px">{unit}</span>
              </div>
              <!-- Source -->
              <div style="font-size:9px;color:#A0A3C0;padding-top:6px;
                          border-top:1px solid #E8E8F8">
                {source_name} · {as_of}
              </div>
            </div>
            """)


# ══════════════════════════════════════════════════════
# NEW: AI Strategy Assistant 렌더링 함수들
# ══════════════════════════════════════════════════════

def _render_daily_signal_summary(macro_data: dict, industry_key: str) -> None:
    """Tab 1 최상단 — 🟢🟡🔴 오늘의 거시경제 신호 + 3줄 브리핑."""
    if not macro_data:
        return
    try:
        signals = detect_macro_signals(macro_data, industry_key)
        summary = get_signal_summary(signals)
    except Exception:
        return

    profile = get_profile(industry_key)
    ind_label = profile.get("label", industry_key)

    # 신호 행 HTML — 라벤더 베이스 + 색상 왼쪽 테두리
    def _sig_row(color: str, emoji: str, label_txt: str, impact_txt: str) -> str:
        _BORDER = {"green": "#16a34a", "yellow": "#d97706", "red": "#dc2626"}
        _FG     = {"green": "#15803d", "yellow": "#92400e", "red": "#991b1b"}
        border  = _BORDER.get(color, "#94a3b8")
        fg      = _FG.get(color, "#475569")
        return (
            f'<div style="display:flex;align-items:flex-start;gap:10px;'
            f'background:#EAEBFF;border-radius:8px;padding:10px 14px;margin-bottom:8px;'
            f'border-left:4px solid {border}">'
            f'<span style="font-size:18px;flex-shrink:0">{emoji}</span>'
            f'<div><span style="font-weight:700;color:{fg};font-size:13px">'
            f'{label_txt}</span>'
            f'<span style="color:#4B4F9A;font-size:12px"> — {impact_txt}</span></div>'
            f'</div>'
        )

    rows_html = ""
    for sig in (summary["green"][:1] + summary["yellow"][:1] + summary["red"][:1]):
        rows_html += _sig_row(
            sig["color"], sig["emoji"],
            sig["signal"],
            sig["impact"],
        )
    if not rows_html:
        for sig in signals[:3]:
            rows_html += _sig_row(sig["color"], sig["emoji"], sig["signal"], sig["impact"])

    # 3줄 브리핑 — 민트그린 번호 + 라벤더 배경
    lines = summary["executive_lines"]
    brief_html = "".join(
        f'<div style="display:flex;gap:10px;align-items:flex-start;'
        f'padding:7px 0;border-bottom:1px solid #D4D5FF;font-size:13px;color:#1E1B4B;line-height:1.6">'
        f'<span style="background:#C8F5D0;color:#166534;border-radius:50%;'
        f'min-width:20px;height:20px;display:inline-flex;align-items:center;'
        f'justify-content:center;font-size:10px;font-weight:800;flex-shrink:0">'
        f'{i+1}</span>'
        f'<span>{line.lstrip("①②③ ")}</span></div>'
        for i, line in enumerate(lines)
    )

    st.html(f"""
    <div style="background:#ffffff;border:1px solid #D4D5FF;border-radius:14px;
                border-top:4px solid #5B5FEE;
                padding:20px 24px;margin-bottom:20px;
                box-shadow:0 2px 12px rgba(91,95,238,0.1)">

      <!-- 섹션 번호 + 제목 -->
      <div style="display:flex;justify-content:space-between;align-items:center;
                  margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="background:#5B5FEE;color:white;min-width:26px;height:26px;
                       border-radius:50%;display:inline-flex;align-items:center;
                       justify-content:center;font-size:12px;font-weight:800">1</span>
          <span style="font-size:14px;font-weight:800;color:#1E1B4B">
            오늘의 거시경제 신호
          </span>
        </div>
        <span style="background:#EAEBFF;color:#5B5FEE;padding:3px 12px;
                     border-radius:20px;font-size:11px;font-weight:700">
          {ind_label}
        </span>
      </div>

      {rows_html}

      <div style="margin-top:16px;padding-top:14px;border-top:2px solid #EAEBFF">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <span style="background:#C8F5D0;color:#166534;padding:2px 10px;
                       border-radius:20px;font-size:10px;font-weight:700">
            📋 오늘의 3줄 브리핑
          </span>
        </div>
        {brief_html}
      </div>
    </div>
    """)


def _render_industry_impact_summary(signals: list, industry_key: str) -> None:
    """산업별 영향 요약 — 상위 3개 신호를 3열 카드로 표시."""
    if not signals:
        return
    try:
        cards = map_industry_impact(signals, top_n=3)
    except Exception:
        return
    if not cards:
        return

    _BG     = {"green": "#EAEBFF", "yellow": "#EAEBFF", "red": "#EAEBFF"}
    _BORDER = {"green": "#16a34a", "yellow": "#d97706", "red": "#dc2626"}
    _FG     = {"green": "#15803d", "yellow": "#92400e", "red": "#991b1b"}

    st.html("""
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <span style="background:#5B5FEE;color:white;min-width:26px;height:26px;
                   border-radius:50%;display:inline-flex;align-items:center;
                   justify-content:center;font-size:12px;font-weight:800;flex-shrink:0">2</span>
      <span style="font-size:14px;font-weight:800;color:#1E1B4B">
        산업별 영향 요약
      </span>
      <span style="font-size:11px;color:#5B5FEE;font-weight:600;
                   background:#EAEBFF;padding:2px 10px;border-radius:20px">
        Industry Impact
      </span>
    </div>
    """)

    cols = st.columns(len(cards), gap="small")
    for card, col in zip(cards, cols):
        color  = card["color"]
        border = _BORDER.get(color, "#94a3b8")
        bg     = _BG.get(color, "#f8fafc")
        fg     = _FG.get(color, "#475569")
        with col:
            st.html(f"""
            <div style="background:{bg};border:1px solid #D4D5FF;
                        border-top:4px solid {border};border-radius:10px;
                        padding:16px;height:100%;min-height:160px;
                        box-shadow:0 2px 8px rgba(91,95,238,0.08)">
              <div style="font-size:12px;font-weight:700;color:{fg};margin-bottom:8px">
                {card['emoji']} {card['macro_signal']}
              </div>
              <div style="font-size:12px;color:#1E1B4B;font-weight:600;
                          margin-bottom:6px;line-height:1.5">
                {card['interpretation']}
              </div>
              <div style="font-size:11px;color:#4B4F9A;margin-bottom:6px;
                          line-height:1.5">
                💼 {card['industry_impact']}
              </div>
              {"" if not card['risk'] or card['risk'] == '—' else
               f'<div style="font-size:11px;color:#dc2626;line-height:1.4">'
               f'⚠️ {card["risk"]}</div>'}
              <div style="font-size:9px;color:#8B8FCF;margin-top:8px;
                          border-top:1px solid #D4D5FF;padding-top:6px">
                {card['source_name']}
                {"&nbsp;|&nbsp;" + card['as_of'] if card['as_of'] else ""}
              </div>
            </div>
            """)


def _render_strategic_insights(macro_data: dict, industry_key: str) -> None:
    """
    전략적 시사점 카드 — 새 포맷 (item 7/12 spec):
      ① Short Strategy Question
      ② Connected Macro Signal
      ③ Industry Impact (1 sentence)
      ④ Action Checklist
    """
    if not macro_data:
        return
    try:
        insights = generate_all_insights(macro_data, industry_key, top_n=3)
    except Exception:
        return
    if not insights:
        return

    _BORDER = {"green": "#16a34a", "yellow": "#d97706", "red": "#dc2626"}
    _BADGE  = {"green": "#C8F5D0", "yellow": "#fef3c7", "red": "#fee2e2"}
    _FG     = {"green": "#166534", "yellow": "#92400e", "red": "#991b1b"}

    # Question templates by signal keyword
    _QUESTION_MAP = {
        "환율":     "이 환율 변동, 우리 업체는 어떻게 대응해야 할까요?",
        "수출":     "수출 동향 변화에 어떤 전략으로 대응해야 할까요?",
        "물가":     "물가 압력 속에서 어떤 원가 관리 전략이 필요할까요?",
        "CPI":      "인플레이션 흐름, 사업에 어떤 영향을 줄까요?",
        "금리":     "금리 변화에 따른 자금 조달 전략은?",
        "수입물가": "수입 원가 상승, 어떻게 대비해야 할까요?",
        "수출물가": "수출 단가 변화, 채산성 점검이 필요한가요?",
    }

    st.html("""
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <span style="background:#5B5FEE;color:white;min-width:26px;height:26px;
                   border-radius:50%;display:inline-flex;align-items:center;
                   justify-content:center;font-size:12px;font-weight:800;flex-shrink:0">3</span>
      <span style="font-size:14px;font-weight:800;color:#1E1B4B">전략적 시사점</span>
      <span style="font-size:11px;color:#5B5FEE;font-weight:600;
                   background:#EAEBFF;padding:2px 10px;border-radius:20px">
        Strategic Insights
      </span>
    </div>
    """)

    for ins in insights:
        color  = ins.get("color", "yellow")
        emoji  = ins.get("emoji", "🟡")
        clabel = ins.get("color_label", "주의")
        label  = ins.get("label", "")
        border = _BORDER.get(color, "#94a3b8")
        bbg    = _BADGE.get(color, "#fef3c7")
        bfg    = _FG.get(color, "#92400e")

        # ① Strategy Question — pick template by keyword
        question = next(
            (q for kw, q in _QUESTION_MAP.items() if kw in label),
            f"{label} 신호, 지금 어떻게 대응해야 할까요?",
        )

        # ② Macro Signal pill
        macro_signal = ins.get("macro_signal", label)

        # ③ Industry Impact — 1 sentence
        impact_txt = ins.get("industry_impact", ins.get("interpretation", ""))

        # ④ Action Checklist
        checklist_html = "".join(
            f'<div style="display:flex;gap:8px;align-items:flex-start;'
            f'padding:5px 0;font-size:12px;color:#374151;'
            f'border-bottom:1px solid {border}22">'
            f'<span style="color:{border};font-size:14px;flex-shrink:0;line-height:1.4">☐</span>'
            f'<span style="line-height:1.5">{item}</span></div>'
            for item in ins.get("action_checklist", [])
        )

        with st.expander(
            f"{emoji} {question}",
            expanded=False,
        ):
            st.html(f"""
            <div style="border:1px solid {border}44;border-radius:12px;
                        overflow:hidden;background:white">

              <!-- ② Connected Macro Signal -->
              <div style="padding:10px 16px;background:{bbg};
                          border-bottom:2px solid {border}33;
                          display:flex;align-items:center;gap:10px">
                <span style="font-size:18px">{emoji}</span>
                <div>
                  <div style="font-size:9px;font-weight:700;color:{bfg};
                              text-transform:uppercase;letter-spacing:.8px">
                    Connected Signal
                  </div>
                  <div style="font-size:13px;font-weight:800;color:{bfg}">
                    {macro_signal}
                  </div>
                </div>
                <span style="margin-left:auto;background:white;color:{bfg};
                             padding:2px 10px;border-radius:20px;font-size:10px;
                             font-weight:700;border:1px solid {border}44">
                  {clabel}
                </span>
              </div>

              <!-- ③ Industry Impact (1 sentence) -->
              <div style="padding:12px 16px;border-bottom:1px solid #f1f5f9">
                <div style="font-size:9px;font-weight:700;color:#8B8FCF;
                            text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px">
                  🏭 Industry Impact
                </div>
                <div style="font-size:13px;color:#1E1B4B;line-height:1.6">
                  {impact_txt}
                </div>
              </div>

              <!-- ④ Action Checklist -->
              <div style="padding:12px 16px;background:#F8F8FF">
                <div style="font-size:9px;font-weight:700;color:#5B5FEE;
                            text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">
                  ✅ Action Checklist
                </div>
                {checklist_html}
              </div>

            </div>
            """)


def _render_industry_focus(industry_key: str) -> None:
    """
    Tab 1 상단 — 선택된 산업 + 거시경제 민감도 태그 배너.
    map_industry_sensitivity()로 고/중/저 민감도를 색상 배지로 표시.
    """
    profile = get_profile(industry_key)
    ind_label = profile.get("label", industry_key)
    ind_icon  = profile.get("icon", "📦")
    ind_desc  = profile.get("description", "")

    try:
        sens = map_industry_sensitivity(industry_key)
    except Exception:
        sens = {}

    # Build sensitivity tags
    _SENS_LABELS = {
        "fx":            "환율",
        "export_growth": "수출",
        "inflation":     "물가",
        "interest_rate": "금리",
    }
    _SENS_COLORS = {
        "high":   ("background:#fee2e2;color:#991b1b;border:1px solid #fca5a5", "높음"),
        "medium": ("background:#fef3c7;color:#92400e;border:1px solid #fcd34d", "중간"),
        "low":    ("background:#f0fdf4;color:#166534;border:1px solid #86efac", "낮음"),
    }
    tags_html = ""
    for dim, dim_label in _SENS_LABELS.items():
        level = sens.get(dim, "medium")
        style, level_ko = _SENS_COLORS.get(level, _SENS_COLORS["medium"])
        tags_html += (
            f'<span style="{style};padding:3px 10px;border-radius:20px;'
            f'font-size:10px;font-weight:700;white-space:nowrap">'
            f'{dim_label} 민감도 {level_ko}</span>'
        )

    desc_html = (
        f'<span style="font-size:11px;color:#4B4F9A;opacity:0.8">{ind_desc}</span>'
        if ind_desc else ""
    )

    st.html(f"""
    <div style="background:linear-gradient(135deg,#EAEBFF 0%,#F4F4FF 100%);
                border:1px solid #D4D5FF;border-left:5px solid #5B5FEE;
                border-radius:12px;padding:14px 20px;margin-bottom:4px;
                display:flex;align-items:center;justify-content:space-between;
                flex-wrap:wrap;gap:12px">
      <!-- Left: industry name -->
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:28px;line-height:1">{ind_icon}</span>
        <div>
          <div style="font-size:9px;font-weight:700;color:#8B8FCF;
                      text-transform:uppercase;letter-spacing:1.2px;margin-bottom:2px">
            Industry Focus
          </div>
          <div style="font-size:16px;font-weight:900;color:#1E1B4B;line-height:1.2">
            {ind_label}
          </div>
          {desc_html}
        </div>
      </div>
      <!-- Right: sensitivity tags -->
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        <span style="font-size:9px;font-weight:600;color:#8B8FCF;
                     text-transform:uppercase;letter-spacing:0.8px;margin-right:2px">
          Macro Sensitivity
        </span>
        {tags_html}
      </div>
    </div>
    """)


def _render_impact_score_banner(macro_data: dict, industry_key: str) -> None:
    """
    Tab 1 상단 — 산업 임팩트 스코어 배너.

    방법론 일관성:
      - 현재 스코어 : trend(▲/▼/→) × 방향성 × 가중치  [-3.0 ~ +3.0]
      - 직전 기간 비교: level(value vs midpoint) × 방향성 × 가중치
                       → 현재/이전 모두 동일 공식 적용 → delta 신뢰 가능
      - 세션 히스토리 : 스파크라인 전용 (delta 계산에서 제외)

    4-row 레이아웃:
      Row 1 — 제목(좌) + 직전 대비 delta 뱃지(우) [변화 없으면 숨김]
      Row 2 — 대형 스코어 + 라벨(좌) + 스파크라인(우)
      Row 2.5 — 한 줄 설명 (왜 우호적/불리한지)
      Row 3 — 구분선 + 긍정/부정 주요인 (방향 포함)
    """
    try:
        from core.impact_scorer import (
            calculate_macro_impact_score,
            update_and_get_score_delta,
            calculate_prev_period_delta,
        )
    except ImportError:
        return
    try:
        result = calculate_macro_impact_score(macro_data, industry_key)
    except Exception:
        return

    score     = result["total"]
    label     = result["label"]
    breakdown = result["breakdown"]

    # ── 지표별 방향 설명 사전 ──────────────────────────────────────────────────
    _IND_NAMES: dict[str, dict[str, str]] = {
        "환율(원/$)":      {"▲": "원화 약세", "▼": "원화 강세",    "→": "환율 안정"},
        "수출증가율":       {"▲": "수출 증가율 상승", "▼": "수출 증가율 하락", "→": "수출 증가율 보합"},
        "기준금리":        {"▲": "금리 인상",  "▼": "금리 인하",   "→": "금리 동결"},
        "소비자물가(CPI)": {"▲": "물가 상승",  "▼": "물가 하락",   "→": "물가 안정"},
        "수출물가지수":     {"▲": "수출 단가 상승", "▼": "수출 단가 하락", "→": "수출 단가 보합"},
        "수입물가지수":     {"▲": "수입 원가 상승", "▼": "수입 원가 하락", "→": "수입 원가 보합"},
        "원/100엔 환율":   {"▲": "엔 강세",    "▼": "엔 약세",     "→": "엔화 보합"},
    }

    def _ind_desc(name: str) -> str:
        trend = macro_data.get(name, {}).get("trend", "→")
        return _IND_NAMES.get(name, {}).get(trend, name)

    def _ko_subj(word: str) -> str:
        """주격 조사 이/가: 마지막 글자 받침 유무로 자동 판별."""
        if not word:
            return "이"
        c = ord(word[-1])
        if 0xAC00 <= c <= 0xD7A3 and (c - 0xAC00) % 28 == 0:
            return "가"   # 받침 없음: 약세, 상승, 안정, 보합 등
        return "이"       # 받침 있음: 인상, 인하, 동결 등

    # ── 주요인 (방향 포함 설명으로 표시) ─────────────────────────────────────
    top_pos_raw = result["top_positive"]
    top_neg_raw = result["top_negative"]
    top_pos = _ind_desc(top_pos_raw) if top_pos_raw != "—" else "—"
    top_neg = _ind_desc(top_neg_raw) if top_neg_raw != "—" else "없음"

    # ── 한 줄 설명 ────────────────────────────────────────────────────────────
    sorted_bd   = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)
    pos_descs   = [_ind_desc(k) for k, v in sorted_bd if v > 0][:2]
    neg_descs   = [_ind_desc(k) for k, v in sorted_bd if v < 0][:2]

    if score >= 2.0:
        if pos_descs:
            _joined = " 및 ".join(pos_descs)
            explanation = f"{_joined}{_ko_subj(_joined)} {industry_key} 수출 환경을 강하게 뒷받침하고 있습니다."
        else:
            explanation = f"{industry_key} 수출 환경이 매우 우호적입니다."
    elif score >= 0.8:
        if pos_descs:
            _joined = ", ".join(pos_descs)
            explanation = f"{_joined}{_ko_subj(_joined)} {industry_key} 수출 환경에 긍정적으로 작용하고 있습니다."
        else:
            explanation = f"{industry_key} 수출 환경이 전반적으로 우호적입니다."
    elif score >= -0.8:
        _parts = []
        if pos_descs: _parts.append(f"{pos_descs[0]}의 긍정 요인")
        if neg_descs: _parts.append(f"{neg_descs[0]}의 부담")
        explanation = (" 속에 ".join(_parts) + "이 혼재하는 중립 국면입니다.") if _parts \
                      else f"{industry_key} 수출 환경이 중립적입니다."
    elif score >= -2.0:
        if neg_descs:
            _joined = ", ".join(neg_descs)
            explanation = f"{_joined}{_ko_subj(_joined)} {industry_key} 수출 환경에 부담으로 작용하고 있습니다."
        else:
            explanation = f"{industry_key} 수출 환경이 비우호적입니다."
    else:
        if neg_descs:
            _joined = " 및 ".join(neg_descs)
            explanation = f"{_joined}{_ko_subj(_joined)} {industry_key} 수출 환경에 강한 압박을 주고 있습니다."
        else:
            explanation = f"{industry_key} 수출 환경이 매우 불리합니다."

    # ── 세션 히스토리 (스파크라인 전용) ──────────────────────────────────────
    try:
        di      = update_and_get_score_delta(industry_key, score)
        history = di["history"]
    except Exception:
        history = [score]

    # ── 직전 기간 대비 delta (PRIMARY — 동일 방법론) ──────────────────────────
    try:
        pd           = calculate_prev_period_delta(macro_data, industry_key)
        prev_delta   = pd["delta"]
        changed_inds = pd["changed_indicators"]
    except Exception:
        prev_delta   = None
        changed_inds = []

    # ── 테마 색상 ─────────────────────────────────────────────────────────────
    if score >= 0.8:
        s_color, bg_color, border = "#16a34a", "#f0fdf4", "#bbf7d0"
    elif score >= -0.8:
        s_color, bg_color, border = "#d97706", "#fffbeb", "#fde68a"
    else:
        s_color, bg_color, border = "#dc2626", "#fef2f2", "#fecaca"

    # ── Delta 뱃지: 변화 없으면 숨김, 변화 있을 때만 표시 ───────────────────
    if prev_delta is not None and prev_delta != 0.0:
        if prev_delta > 0:
            d_icon, d_color, d_val = "&#9650;", "#16a34a", f"+{prev_delta}"
        else:
            d_icon, d_color, d_val = "&#9660;", "#dc2626", str(prev_delta)
        changed_tag = (
            f'<div style="font-size:9px;color:#9ca3af;margin-top:2px;text-align:right;">'
            f'전환: {", ".join(changed_inds[:2])}{"…" if len(changed_inds) > 2 else ""}</div>'
        ) if changed_inds else ""
        delta_html = (
            f'<div>'
            f'<span style="display:inline-block;font-size:12px;font-weight:700;'
            f'color:{d_color};background:rgba(0,0,0,.07);padding:4px 12px;border-radius:20px;">'
            f'{d_icon} {d_val} 직전 대비</span>'
            f'{changed_tag}</div>'
        )
    else:
        # ±0.0이거나 prev_value 없음 → 뱃지 숨김
        delta_html = ""

    # ── SVG 스파크라인 ────────────────────────────────────────────────────────
    def _sparkline_svg(scores: list, w: int = 72, h: int = 28) -> str:
        if len(scores) < 2:
            return ""
        n   = len(scores)
        lo  = min(scores) - 0.2
        hi  = max(scores) + 0.2
        rng = hi - lo if hi != lo else 1.0
        xs  = [round(i / (n - 1) * w, 1) for i in range(n)]
        ys  = [round((1 - (s - lo) / rng) * h, 1) for s in scores]
        pts = " ".join(f"{x},{y}" for x, y in zip(xs, ys))
        lc  = "#16a34a" if scores[-1] >= 0.8 else ("#dc2626" if scores[-1] < -0.8 else "#d97706")
        return (
            f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="overflow:visible;">'
            f'<polyline points="{pts}" fill="none" stroke="{lc}" '
            f'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{xs[-1]}" cy="{ys[-1]}" r="3" fill="{lc}"/>'
            f'</svg>'
        )

    spark = _sparkline_svg(history)
    spark_block = (
        f'<div style="display:flex;flex-direction:column;align-items:center;gap:3px;'
        f'padding:6px 10px;background:rgba(0,0,0,.04);border-radius:8px;">'
        f'{spark}'
        f'<span style="font-size:9px;color:#9ca3af;white-space:nowrap;">추이 {len(history)}회</span>'
        f'</div>'
    ) if spark else ""

    # ── 부정 요인 색상 ─────────────────────────────────────────────────────────
    neg_color    = "#dc2626" if top_neg != "없음" else "#9ca3af"
    # Row 1 헤더 영역: delta 없으면 제목만 표시 (flex 유지)
    header_right = delta_html if delta_html else ""

    # ── 메인 배너 (4-row 구조) ────────────────────────────────────────────────
    st.html(f"""
    <div style="background:{bg_color};border:1px solid {border};
                border-left:5px solid {s_color};border-radius:10px;
                padding:14px 20px;margin-bottom:6px;">

      <!-- Row 1: 제목(좌) + delta 뱃지(우, 변화 있을 때만) -->
      <div style="display:flex;justify-content:space-between;align-items:flex-start;
                  margin-bottom:8px;">
        <div style="font-size:10px;color:#6b7280;font-weight:700;
                    text-transform:uppercase;letter-spacing:.8px;padding-top:3px;">
          {industry_key} 산업 임팩트 스코어
        </div>
        {header_right}
      </div>

      <!-- Row 2: 대형 스코어 + 라벨(좌) + 스파크라인(우) -->
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">
          <span style="font-size:42px;font-weight:900;color:{s_color};line-height:1;">
            {score:+.1f}
          </span>
          <span style="font-size:15px;color:{s_color};font-weight:600;">
            {label}
          </span>
        </div>
        {spark_block}
      </div>

      <!-- Row 2.5: 한 줄 설명 -->
      <div style="margin-top:6px;font-size:11px;color:#4b5563;font-style:italic;
                  line-height:1.5;">
        {explanation}
      </div>

      <!-- Row 3: 구분선 + 주요인 (방향 포함) -->
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid {border};
                  display:flex;gap:28px;font-size:11px;color:#374151;flex-wrap:wrap;">
        <span>&#128200; 긍정 요인: <strong style="color:#16a34a;">{top_pos}</strong></span>
        <span>&#128201; 부정 요인: <strong style="color:{neg_color};">{top_neg}</strong></span>
      </div>

    </div>
    """)

    # ── 지표별 기여도 상세 (접힌 상태) ───────────────────────────────────────
    with st.expander("📊 지표별 기여도 상세", expanded=False):
        sorted_items = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)
        if sorted_items:
            cells_html = "".join(
                f'<div style="flex:1;min-width:90px;text-align:center;padding:8px 4px;'
                f'background:#f9fafb;border-radius:6px;'
                f'border-top:3px solid {"#16a34a" if c > 0 else ("#dc2626" if c < 0 else "#6b7280")};'
                f'margin:0 3px;">'
                f'<div style="font-size:10px;color:#6b7280;margin-bottom:3px;word-break:keep-all;">{k}</div>'
                f'<div style="font-size:17px;font-weight:800;'
                f'color:{"#16a34a" if c > 0 else ("#dc2626" if c < 0 else "#6b7280")};">'
                f'{"&#9650;" if c > 0 else ("&#9660;" if c < 0 else "&#8594;")}{abs(c):.2f}</div>'
                f'<div style="font-size:9px;margin-top:3px;'
                f'color:{"#16a34a" if c > 0 else ("#dc2626" if c < 0 else "#6b7280")};">'
                f'{"수출 유리" if c > 0 else ("수출 불리" if c < 0 else "중립")}</div>'
                f'</div>'
                for k, c in sorted_items
            )
            st.html(f'<div style="display:flex;flex-wrap:wrap;gap:4px;padding:4px 0;">{cells_html}</div>')


def _render_kpi_section_v2(industry_key: str) -> None:
    """
    Primary KPI 카드 v2 — 새 계층 구조 (item 3 spec):
      1. Large Number + trend arrow
      2. Indicator Name (uppercase)
      3. Signal Badge  (🟢 Opportunity / 🟡 Caution / 🔴 Risk)
      4. Short Insight (1 sentence, AI-generated or rule-based)
      5. Source: ... | Updated: ...
    """
    if not _MACRO:
        st.info("거시지표 데이터 없음 — ECOS API 키 설정 후 업데이트 버튼을 클릭하세요.")
        return

    # ── Primary indicators only (item 6) ──────────────────────────
    PRIMARY = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
    items = [(k, _MACRO[k]) for k in PRIMARY if k in _MACRO]
    if not items:
        return

    # Design tokens
    _BORDER  = {"green": "#16a34a", "yellow": "#d97706", "red": "#dc2626"}
    _BADGE   = {"green": "#C8F5D0", "yellow": "#fef3c7", "red": "#fee2e2"}
    _BADGE_FG= {"green": "#166534", "yellow": "#92400e", "red": "#991b1b"}
    _TC      = {"▲": "#16a34a",    "▼": "#dc2626",      "→": "#94a3b8"}
    _TI      = {"▲": "↑",          "▼": "↓",             "→": "→"}

    try:
        _signals = detect_macro_signals(_MACRO, industry_key)
        _sig_map = {s["label"]: s for s in _signals}
    except Exception:
        _sig_map = {}

    cols = st.columns(len(items), gap="small")
    for (label, data), col in zip(items, cols):
        with col:
            trend       = data.get("trend", "→")
            unit        = data.get("unit", "")
            val_str     = _fmt_value(label, data.get("value", ""))
            as_of       = data.get("as_of", "")
            source_name = data.get("source_name", "한국은행 ECOS")

            sig         = _sig_map.get(label, {})
            color       = sig.get("color", "yellow")

            # ── Signal type: Opportunity / Caution / Risk ──────────
            direction   = calculate_impact_direction(label, trend, industry_key, color)
            dir_en      = get_direction_en(direction)     # "Opportunity" | "Caution" | "Risk"
            dir_emoji   = get_direction_emoji(direction)  # 🟢 🟡 🔴

            # ── AI or rule-based short insight (1 sentence) ────────
            ai_insight  = generate_ai_insight(
                label, trend, industry_key, direction, use_llm=False
            )
            # Truncate to 1 sentence
            ai_insight  = ai_insight.split(".")[0] + "." if "." in ai_insight else ai_insight

            border = _BORDER.get(color, "#94a3b8")
            bbg    = _BADGE.get(color, "#EAEBFF")
            bfg    = _BADGE_FG.get(color, "#4B4F9A")
            tc     = _TC.get(trend, "#94a3b8")
            ti     = _TI.get(trend, "→")

            # Core indicator badge
            _weights = get_profile(industry_key).get("macro_weights", {})
            is_key = _weights.get(label, 0) >= 1.5
            core_badge = (
                '<span style="background:#5B5FEE;color:white;padding:1px 8px;'
                'border-radius:10px;font-size:9px;font-weight:700">CORE</span>'
            ) if is_key else ""

            # Short label for display
            _SHORT = {
                "환율(원/$)":      "USD/KRW",
                "소비자물가(CPI)": "CPI",
                "수출증가율":      "Export Growth",
                "기준금리":        "Interest Rate",
            }
            short_label = _SHORT.get(label, label)

            st.html(f"""
            <div style="background:#EAEBFF;border:1px solid #D4D5FF;
                        border-top:5px solid {border};border-radius:14px;
                        padding:20px 18px 16px;
                        box-shadow:0 2px 12px rgba(91,95,238,0.1);
                        display:flex;flex-direction:column;gap:0">

              <!-- 1. LARGE NUMBER + Trend -->
              <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
                <span style="font-size:2.8rem;font-weight:900;color:#1E1B4B;
                             line-height:1;letter-spacing:-2px">{val_str}</span>
                <span style="font-size:1.5rem;font-weight:800;color:{tc};
                             line-height:1">{ti}</span>
              </div>

              <!-- 2. Indicator Name + unit -->
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
                <span style="font-size:11px;font-weight:800;color:#4B4F9A;
                             text-transform:uppercase;letter-spacing:1px">{short_label}</span>
                <span style="font-size:10px;color:#8B8FCF">{unit}</span>
                {core_badge}
              </div>

              <!-- 3. Signal Badge -->
              <div style="margin-bottom:10px">
                <span style="background:{bbg};color:{bfg};padding:3px 12px;
                             border-radius:20px;font-size:11px;font-weight:700;
                             border:1px solid {border}22">
                  {dir_emoji} {dir_en}
                </span>
              </div>

              <!-- 4. Short Insight (1 sentence) -->
              <div style="font-size:12px;color:#1E1B4B;line-height:1.5;
                          flex:1;margin-bottom:10px">
                {ai_insight}
              </div>

              <!-- 5. Source + Date -->
              <div style="font-size:10px;color:#8B8FCF;padding-top:8px;
                          border-top:1px solid #D4D5FF">
                Source: {source_name}<br>
                Updated: {as_of}
              </div>
            </div>
            """)


def _render_industry_comparison(macro_data: dict) -> None:
    """
    전 산업 거시경제 영향 비교표 — expander 안에 표시.
    [Item 2] 종합 임팩트 스코어 컬럼 추가 (수치 + 색상).
    """
    if not macro_data:
        return
    try:
        rows = get_industry_comparison(macro_data)
    except Exception:
        return
    if not rows:
        return

    # 산업별 종합 임팩트 스코어 계산
    try:
        from core.impact_scorer import calculate_macro_impact_score as _cms
        _score_map = {}
        for row in rows:
            try:
                r = _cms(macro_data, row["key"])
                _score_map[row["key"]] = (r["total"], r["label"])
            except Exception:
                _score_map[row["key"]] = (0.0, "중립 🟡")
    except ImportError:
        _score_map = {}

    _COLOR_CELL = {
        "green":  ("🟢", "#f0fdf4", "#15803d"),
        "yellow": ("🟡", "#fffbeb", "#92400e"),
        "red":    ("🔴", "#fef2f2", "#991b1b"),
    }
    _SCORE_STYLE = lambda s: (
        ("#f0fdf4", "#16a34a") if s >= 0.8 else
        ("#fef2f2", "#dc2626") if s < -0.8 else
        ("#fffbeb", "#d97706")
    )

    with st.expander("🔍 전 산업 거시경제 영향 비교", expanded=False):
        st.html("""
        <div style="font-size:11px;color:#64748b;margin-bottom:8px">
          환율 · 금리 · 수요 차원의 질적 신호와 종합 임팩트 스코어(-3.0 ~ +3.0)를 함께 표시합니다.
        </div>
        """)

        header_html = """
        <tr style="background:#EAEBFF">
          <th style="text-align:left;padding:10px 14px;font-size:11px;
                     color:#4B4F9A;font-weight:700;border-bottom:2px solid #C8C9FF">산업</th>
          <th style="text-align:center;padding:10px 14px;font-size:11px;
                     color:#4B4F9A;font-weight:700;border-bottom:2px solid #C8C9FF">종합 임팩트</th>
          <th style="text-align:center;padding:10px 14px;font-size:11px;
                     color:#4B4F9A;font-weight:700;border-bottom:2px solid #C8C9FF">환율</th>
          <th style="text-align:center;padding:10px 14px;font-size:11px;
                     color:#4B4F9A;font-weight:700;border-bottom:2px solid #C8C9FF">금리</th>
          <th style="text-align:center;padding:10px 14px;font-size:11px;
                     color:#4B4F9A;font-weight:700;border-bottom:2px solid #C8C9FF">수요</th>
        </tr>
        """
        rows_html = ""
        for i, row in enumerate(rows):
            bg = "#ffffff" if i % 2 == 0 else "#fafafa"

            def _cell(color_key: str, text: str) -> str:
                em, cbg, cfg = _COLOR_CELL.get(color_key, ("🟡", "#fffbeb", "#92400e"))
                return (
                    f'<td style="text-align:center;padding:8px 10px">'
                    f'<span style="background:{cbg};color:{cfg};padding:2px 8px;'
                    f'border-radius:12px;font-size:11px;font-weight:600">'
                    f'{em} {text}</span></td>'
                )

            score_val, _ = _score_map.get(row["key"], (0.0, "중립 🟡"))
            sbg, sfg = _SCORE_STYLE(score_val)
            score_cell = (
                f'<td style="text-align:center;padding:8px 10px">'
                f'<span style="background:{sbg};color:{sfg};padding:3px 10px;'
                f'border-radius:12px;font-size:12px;font-weight:800;">'
                f'{score_val:+.1f}</span></td>'
            )

            rows_html += (
                f'<tr style="background:{bg};border-bottom:1px solid #f1f5f9">'
                f'<td style="padding:8px 12px;font-size:12px;font-weight:600;color:#1e293b">'
                f'{row["icon"]} {row["label"]}</td>'
                + score_cell
                + _cell(row["fx_color"],     row["fx_impact"])
                + _cell(row["rate_color"],   row["rate_impact"])
                + _cell(row["demand_color"], row["demand_trend"])
                + "</tr>"
            )

        st.html(f"""
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:12px;
                        border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
            <thead>{header_html}</thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        """)


# ══════════════════════════════════════════════════════
# [Item 4] 임계값 알림 시스템
# ══════════════════════════════════════════════════════
# 주요 지표가 임계값을 넘을 때 Tab 1 최상단에 조용한 알림 표시
_ALERT_RULES: list[tuple] = [
    # (지표명, 조건함수, 알림문구_템플릿, 심각도)
    ("환율(원/$)",       lambda v: v > 1450,
     "환율 {v:.0f}원 초과 — 달러 결제 조건 및 헤지 비율 즉시 재검토 필요", "critical"),
    ("환율(원/$)",       lambda v: 1380 < v <= 1450,
     "환율 {v:.0f}원대 진입 — 수출계약 달러 비중 및 환노출 점검 권고", "warning"),
    ("기준금리",          lambda v: v >= 3.5,
     "기준금리 {v:.2f}% — 자금조달 비용 증가 구간, 단기 차입 구조 재검토", "warning"),
    ("소비자물가(CPI)",  lambda v: v >= 3.0,
     "소비자물가 {v:.1f}% — 원가·인건비 압박 구간, 수출 마진 점검 필요", "warning"),
    ("수출증가율",        lambda v: v < -5.0,
     "수출증가율 {v:.1f}% — 수출 감소 국면, 주요 시장 다변화 전략 점검", "critical"),
    ("수입물가지수",      lambda v: v > 5.0,
     "수입물가 {v:.1f}% 상승 — 원자재 조달 비용 압박, 계약 조건 재협상 검토", "warning"),
]


def _render_threshold_alerts(macro_data: dict, industry_key: str) -> None:
    """
    핵심 지표가 임계값을 초과할 때 Tab 1 최상단에 조용한 경보 배너 표시.
    - critical: 붉은 배경, 즉시 조치 필요
    - warning: 노란 배경, 모니터링 권고
    """
    if not macro_data:
        return

    triggered: list[tuple[str, str]] = []  # (severity, text)
    for label, rule_fn, template, severity in _ALERT_RULES:
        item = macro_data.get(label)
        if not item or not isinstance(item, dict):
            continue
        try:
            val = float(str(item.get("value", "0")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue
        if rule_fn(val):
            triggered.append((severity, template.format(v=val)))
            break  # 같은 지표에서 최고 우선순위만 표시

    if not triggered:
        return

    for severity, text in triggered:
        if severity == "critical":
            bg, border, icon, tc = "#fef2f2", "#f87171", "🚨", "#991b1b"
        else:
            bg, border, icon, tc = "#fffbeb", "#fcd34d", "⚠️", "#92400e"
        st.html(f"""
        <div style="background:{bg};border:1px solid {border};border-left:4px solid {border};
                    border-radius:8px;padding:10px 16px;margin-bottom:8px;
                    display:flex;align-items:center;gap:10px;">
          <span style="font-size:16px;">{icon}</span>
          <span style="font-size:12px;font-weight:600;color:{tc};">{text}</span>
        </div>
        """)


# ══════════════════════════════════════════════════════
# [Item 3] Signal-to-Action 브리핑 카드
# ══════════════════════════════════════════════════════
def _render_action_briefing(
    macro_data: dict,
    industry_key: str,
    signals: list[dict],
) -> None:
    """
    임팩트 스코어 배너 직후 — '지금 해야 할 것' 3개 액션 카드.
    signals의 action 필드를 활용하여 rule-based로 생성.
    """
    if not macro_data or not signals:
        return
    try:
        from core.impact_scorer import calculate_macro_impact_score
        result = calculate_macro_impact_score(macro_data, industry_key)
        score = result["total"]
    except Exception:
        score = 0.0

    # 스코어 기반 환경 요약 문구
    if score >= 2.0:
        env_line = "현재 수출 환경이 매우 우호적입니다. 이 모멘텀을 활용할 시점입니다."
        header_color, header_bg = "#166534", "#f0fdf4"
    elif score >= 0.8:
        env_line = "수출 환경이 우호적입니다. 기회 요인을 최대화하고 리스크를 선제 관리하세요."
        header_color, header_bg = "#166534", "#f0fdf4"
    elif score >= -0.8:
        env_line = "수출 환경이 중립 구간입니다. 변화 방향에 따라 선제 대응이 중요합니다."
        header_color, header_bg = "#92400e", "#fffbeb"
    else:
        env_line = "수출 환경에 압박 요인이 있습니다. 우선순위 대응 사항을 확인하세요."
        header_color, header_bg = "#991b1b", "#fef2f2"

    # 상위 액션 추출: 가중치 높은 순으로 정렬, action 있는 것만
    _COLOR_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    top = sorted(
        [s for s in signals if s.get("action", "").strip()],
        key=lambda s: s.get("weight", 0),
        reverse=True,
    )[:3]

    if not top:
        return

    action_rows = ""
    for sig in top:
        icon = _COLOR_ICON.get(sig.get("color", "yellow"), "🟡")
        trend = sig.get("trend", "→")
        lbl   = sig.get("label", "")
        act   = sig.get("action", "")
        action_rows += (
            f'<div style="display:flex;align-items:flex-start;gap:10px;'
            f'padding:8px 0;border-bottom:1px solid rgba(0,0,0,.06);">'
            f'<span style="font-size:14px;flex-shrink:0;">{icon}</span>'
            f'<div>'
            f'<div style="font-size:10px;color:#6b7280;font-weight:600;margin-bottom:2px;">'
            f'{lbl} {trend}</div>'
            f'<div style="font-size:12px;color:#1e293b;font-weight:500;">{act}</div>'
            f'</div></div>'
        )

    st.html(f"""
    <div style="background:{header_bg};border:1px solid rgba(0,0,0,.08);
                border-radius:10px;padding:14px 18px;margin-bottom:6px;">
      <div style="font-size:10px;font-weight:700;color:{header_color};
                  text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;">
        ⚡ 지금 해야 할 것 — {industry_key}
      </div>
      <div style="font-size:11px;color:#374151;margin-bottom:10px;font-style:italic;">
        {env_line}
      </div>
      {action_rows}
    </div>
    """)


# ══════════════════════════════════════════════════════
# [Item 1] 산업별 임팩트 랭킹
# ══════════════════════════════════════════════════════
_RANK_INDUSTRIES = ["반도체", "자동차", "배터리", "조선", "화학", "철강", "소비재"]


def _render_industry_impact_ranking(macro_data: dict) -> None:
    """
    전 산업 임팩트 스코어 순위표 — 비교표 아래에 배치.
    calculate_macro_impact_score로 실시간 산출 후 내림차순 정렬.
    """
    if not macro_data:
        return
    try:
        from core.impact_scorer import calculate_macro_impact_score
        from core.industry_config import get_profile
    except ImportError:
        return

    ranked = []
    for ind in _RANK_INDUSTRIES:
        try:
            r = calculate_macro_impact_score(macro_data, ind)
            profile = get_profile(ind)
            ranked.append({
                "key":   ind,
                "label": profile.get("label", ind),
                "icon":  profile.get("icon", "📦"),
                "score": r["total"],
                "lbl":   r["label"],
            })
        except Exception:
            continue
    ranked.sort(key=lambda x: x["score"], reverse=True)

    if not ranked:
        return

    # 메달 / 색상
    _MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}
    _SCORE_COLOR = lambda s: "#16a34a" if s >= 0.8 else ("#dc2626" if s < -0.8 else "#d97706")
    _SCORE_BG    = lambda s: "#f0fdf4" if s >= 0.8 else ("#fef2f2" if s < -0.8 else "#fffbeb")
    _BAR_W       = lambda s: max(4, int((s + 3) / 6 * 100))  # 0~100%

    rows_html = ""
    for i, item in enumerate(ranked):
        s     = item["score"]
        medal = _MEDALS.get(i, f'<span style="font-size:11px;color:#6b7280;font-weight:700;">{i+1}</span>')
        bar_w = _BAR_W(s)
        sc    = _SCORE_COLOR(s)
        sb    = _SCORE_BG(s)
        rows_html += (
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'padding:8px 12px;border-bottom:1px solid #f1f5f9;'
            f'background:{"#fafffe" if i % 2 == 0 else "#ffffff"};">'
            f'<div style="width:28px;text-align:center;font-size:16px;">{medal}</div>'
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:12px;font-weight:600;color:#1e293b;">'
            f'{item["icon"]} {item["label"]}</div>'
            f'<div style="margin-top:4px;height:5px;background:#f1f5f9;border-radius:3px;">'
            f'<div style="width:{bar_w}%;height:100%;background:{sc};border-radius:3px;'
            f'transition:width .3s;"></div></div>'
            f'</div>'
            f'<div style="background:{sb};color:{sc};font-size:13px;font-weight:800;'
            f'padding:4px 12px;border-radius:20px;white-space:nowrap;min-width:52px;text-align:center;">'
            f'{s:+.1f}</div>'
            f'</div>'
        )

    st.html(f"""
    <div style="border:1px solid #e2e8f0;border-radius:10px;
                overflow:hidden;margin-top:8px;">
      <div style="background:#EAEBFF;padding:10px 16px;
                  display:flex;justify-content:space-between;align-items:center;">
        <span style="font-size:12px;font-weight:700;color:#4B4F9A;">
          🏆 산업별 임팩트 랭킹
        </span>
        <span style="font-size:10px;color:#6b7280;">현재 거시경제 기준 | -3.0 ~ +3.0</span>
      </div>
      {rows_html}
    </div>
    """)


# ══════════════════════════════════════════════════════
# 4-A. 오늘의 핵심 신호 카드
# ══════════════════════════════════════════════════════

def _section_header(title: str, subtitle: str = "", number: str = "") -> None:
    """통일된 섹션 헤더 렌더링 — accent bar + 번호 뱃지 + 서브타이틀."""
    badge_html = (
        f'<span style="background:#5B5FEE;color:white;min-width:24px;height:24px;'
        f'border-radius:50%;display:inline-flex;align-items:center;'
        f'justify-content:center;font-size:11px;font-weight:800;'
        f'flex-shrink:0;margin-right:8px">{number}</span>'
        if number else ""
    )
    sub_html = (
        f'<div style="font-size:12px;color:#64748b;margin-top:3px">{subtitle}</div>'
        if subtitle else ""
    )
    st.html(f"""
    <div style="display:flex;align-items:flex-start;
                margin-top:4px;margin-bottom:12px;
                padding-left:14px;
                border-left:4px solid #5B5FEE;
                font-family:'Inter',sans-serif">
      <div>
        <div style="display:flex;align-items:center">
          {badge_html}
          <span style="font-size:18px;font-weight:800;color:#1E1B4B">{title}</span>
        </div>
        {sub_html}
      </div>
    </div>
    """)

def _render_kpi_section_card(kpi_items: list) -> None:
    """KPI 4종을 white card 그리드로 렌더링 (st.metric 대체)."""
    cards_html = ""
    for label, data in kpi_items:
        val    = data.get("value", "—")
        unit   = data.get("unit", "")
        trend  = data.get("trend", "→")
        prev   = data.get("prev_value", "")
        # delta 계산
        try:
            _v = float(str(val).replace(",", "").replace("+", ""))
            _p = float(str(prev).replace(",", "").replace("+", ""))
            _delta = round(_v - _p, 2)
            _delta_color = "#dc2626" if _delta > 0 else "#2563eb" if _delta < 0 else "#6b7280"
            delta_html = (
                f'<div style="margin-top:6px;font-size:12px;font-weight:600;color:{_delta_color}">'
                f'{_delta:+.2f} {unit}</div>'
            )
        except Exception:
            delta_html = ""
        trend_color = "#dc2626" if trend == "▲" else "#2563eb" if trend == "▼" else "#6b7280"
        # threshold 상태 배지
        _status, _, _status_label = _get_threshold_status(label, str(val))
        _badge_map = {
            "danger":  ('<span style="background:#fef2f2;color:#dc2626;font-size:10px;'
                        'font-weight:700;padding:2px 7px;border-radius:8px;'
                        'border:1px solid #fecaca">⚠ 위험</span>'),
            "warning": ('<span style="background:#fff7ed;color:#f97316;font-size:10px;'
                        'font-weight:700;padding:2px 7px;border-radius:8px;'
                        'border:1px solid #fed7aa">⚠ 경고</span>'),
            "caution": ('<span style="background:#fefce8;color:#ca8a04;font-size:10px;'
                        'font-weight:700;padding:2px 7px;border-radius:8px;'
                        'border:1px solid #fde68a">주의</span>'),
        }
        badge_html = _badge_map.get(_status, "")
        _fmt_val = _fmt_value(label, val)
        cards_html += f"""
        <div style="background:#ffffff;border:1px solid #E2E8F0;border-radius:14px;
                    padding:20px 18px 16px;text-align:center;
                    box-shadow:0 1px 4px rgba(0,0,0,0.05);
                    transition:box-shadow 0.2s">
          <div style="font-size:12px;font-weight:600;color:#64748b;
                      margin-bottom:10px;letter-spacing:0.3px">{label}</div>
          <div style="font-size:28px;font-weight:800;color:#1E1B4B;line-height:1">
            {_fmt_val}
          </div>
          <div style="font-size:13px;color:#64748b;margin-top:2px">
            {unit}&nbsp;<span style="color:{trend_color};font-size:16px">{trend}</span>
          </div>
          {delta_html}
          <div style="margin-top:8px">{badge_html}</div>
        </div>"""

    st.html(f"""
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;
                margin-bottom:8px;font-family:'Inter',sans-serif">
      {cards_html}
    </div>
    """)

def _render_today_signal(industry_key: str) -> None:
    """탭 위에 '오늘의 핵심 경제 신호' Hero Card 렌더링."""
    signal = generate_today_signal(_MACRO, industry_key)
    if not signal:
        return

    profile = get_profile(industry_key)
    trend_color = "#dc2626" if signal["trend"] == "▲" else "#2563eb" if signal["trend"] == "▼" else "#6b7280"

    # Impact Detail 3분류
    detail = signal.get("impact_detail", {})
    impact_text = detail.get("impact", signal["impact"])
    risk_text = detail.get("risk", "")
    opportunity_text = detail.get("opportunity", "")

    # 체크리스트: 첫 번째만 Hero에 표시, 나머지는 접힘
    checklist = signal.get("checklist", [])
    first_action = checklist[0] if checklist else ""
    extra_checklist = checklist[1:]
    extra_html = ""
    if extra_checklist:
        extra_items = "".join(
            f'<div style="margin:4px 0;font-size:13px;color:#475569">📌 {item}</div>'
            for item in extra_checklist
        )
        extra_html = f"""
        <details style="margin-top:8px">
          <summary style="font-size:13px;color:#5B5FEE;cursor:pointer;font-weight:600">
            + 추가 확인 항목 보기 ({len(extra_checklist)}개)
          </summary>
          <div style="margin-top:6px">{extra_items}</div>
        </details>"""

    st.html(f"""
    <div style="background:linear-gradient(135deg,#eff6ff 0%,#e0e7ff 100%);
                border:2px solid #5B5FEE;border-radius:20px;
                padding:28px 32px;margin-bottom:20px;
                font-family:'Inter',sans-serif;position:relative">
      <!-- 헤더: 제목 + 산업 배지 -->
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <div style="font-size:14px;font-weight:700;color:#5B5FEE">
          ⚡ 오늘의 핵심 신호
        </div>
        <div style="background:#5B5FEE;color:#fff;font-size:12px;font-weight:600;
                    padding:4px 12px;border-radius:20px">
          {profile['icon']} {profile['label']}
        </div>
      </div>

      <!-- 지표 값 -->
      <div style="font-size:26px;font-weight:800;color:#1e293b;margin-bottom:20px">
        {signal['label']}&nbsp;&nbsp;
        <span style="font-size:28px">{signal['value']}</span>&nbsp;
        <span style="color:{trend_color};font-size:24px">{signal['trend']}</span>
      </div>

      <!-- Impact / Risk / Opportunity 3컬럼 -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px">
        <div style="background:rgba(59,130,246,0.08);border-radius:12px;padding:14px 16px">
          <div style="font-size:12px;font-weight:700;color:#3B82F6;margin-bottom:6px">
            📊 Impact
          </div>
          <div style="font-size:13px;color:#1e40af;line-height:1.5">{impact_text}</div>
        </div>
        <div style="background:rgba(239,68,68,0.08);border-radius:12px;padding:14px 16px">
          <div style="font-size:12px;font-weight:700;color:#EF4444;margin-bottom:6px">
            📉 Risk
          </div>
          <div style="font-size:13px;color:#991b1b;line-height:1.5">{risk_text}</div>
        </div>
        <div style="background:rgba(34,197,94,0.08);border-radius:12px;padding:14px 16px">
          <div style="font-size:12px;font-weight:700;color:#22C55E;margin-bottom:6px">
            💡 Opportunity
          </div>
          <div style="font-size:13px;color:#166534;line-height:1.5">{opportunity_text}</div>
        </div>
      </div>

      <!-- 즉시 행동 1개 -->
      {"" if not first_action else f'''
      <div style="background:rgba(91,95,238,0.08);border-radius:10px;padding:12px 16px;
                  border-left:4px solid #5B5FEE">
        <span style="font-size:13px;font-weight:700;color:#5B5FEE">✦ 즉시 행동 :</span>
        <span style="font-size:13px;color:#334155;margin-left:6px">{first_action}</span>
      </div>'''}

      <!-- 추가 체크리스트 (접힘) -->
      {extra_html}
    </div>
    """)


# ══════════════════════════════════════════════════════
# 4-A2. Impact Strip (신호등 스트립)
# ══════════════════════════════════════════════════════
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


def _render_impact_strip() -> None:
    """Hero Card 아래, KPI 위에 3개 지표 신호등 스트립 렌더링."""
    if not _MACRO:
        return

    cells = []
    for macro_key, display_name in _IMPACT_STRIP_INDICATORS:
        data = _MACRO.get(macro_key)
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


# ══════════════════════════════════════════════════════
# 4-B. 산업별 핵심 변수 카드
# ══════════════════════════════════════════════════════
def _render_industry_variable_card(industry_key: str, docs: list) -> None:
    """Tab 1 상단에 산업별 핵심 변수 카드 표시."""
    if industry_key == "일반":
        return

    profile = get_profile(industry_key)
    cv_list = profile["critical_variables"]

    items_html = ""
    for cv in cv_list:
        # 거시지표와 매칭되는 변수는 현재값 표시
        macro_match = _MACRO.get(cv)
        if macro_match:
            val = macro_match.get("value", "")
            trend = macro_match.get("trend", "")
            status, _, status_label = _get_threshold_status(cv, str(val))
            status_badge = f' <span style="color:#dc2626;font-size:11px">⚠️{status_label}</span>' if status in ("warning", "danger", "caution") else ""
            items_html += f'<div style="margin:4px 0;font-size:13px">📌 {cv} → {val} {trend}{status_badge}</div>'
        else:
            # 기사 매칭 수 카운트
            count = sum(1 for d in docs if cv.replace("(", "").replace(")", "") in d.get("title", ""))
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


# ══════════════════════════════════════════════════════
# 4. render_ui — 메인 UI (Infographic Dashboard)
# ══════════════════════════════════════════════════════
_KDI_URL = "https://eiec.kdi.re.kr/publish/naraList.do"


def render_ui() -> None:
    # ── 페이지 뷰 로깅 (세션당 1회) ──────────────────
    if "page_view_logged" not in st.session_state:
        try:
            _utm_source = st.query_params.get("utm_source", "")
            _utm_article = st.query_params.get("article_id", "")
            log_event("page_view", detail={
                "utm_source": _utm_source,
                "article_id": _utm_article,
            } if _utm_source else None)
            # 이메일 링크를 통한 접속 시 email_click 이벤트도 기록
            if _utm_source == "email":
                log_event("email_click", detail={
                    "utm_campaign": st.query_params.get("utm_campaign", ""),
                    "article_id": _utm_article,
                })
        except Exception:
            log_event("page_view")
        st.session_state["page_view_logged"] = True

    # ── 사이드바: 산업 선택 + Fake Door ───────────────
    _industry_list = get_industry_list()
    with st.sidebar:
        st.markdown("### 🏭 산업 선택")
        _ind_options = [item["key"] for item in _industry_list]
        _ind_labels  = {item["key"]: f'{get_profile(item["key"])["icon"]} {item["label"]}' for item in _industry_list}
        _sel_ind = st.selectbox(
            "산업을 선택하세요",
            options=_ind_options,
            index=_ind_options.index(st.session_state.get("selected_industry", "일반")),
            format_func=lambda k: _ind_labels[k],
            key="_industry_sb",
        )
        if st.session_state.get("selected_industry") != _sel_ind:
            log_event("industry_select", {"industry": _sel_ind})
            # 산업 변경 시 기사 캐시 초기화 → 새 산업으로 re-fetch
            st.session_state.pop("docs", None)
            st.session_state.pop("docs_others", None)
            st.session_state.pop("docs_fetched_at", None)
            st.session_state.pop("selected_id", None)
            st.session_state.pop("last_doc", None)
            st.session_state.pop("last_detail", None)
            # V17.8: 산업 변경 시 서브카테고리 강제 리셋
            st.session_state["selected_subcategory"] = "전체"
        st.session_state["selected_industry"] = _sel_ind
        _profile = get_profile(_sel_ind)

        # V17.8: 서브카테고리 셀렉터 (산업 선택 아래)
        _sub_list = get_subcategory_list(_sel_ind)
        if len(_sub_list) > 1:
            _sub_labels = {s: get_subcategory_label(_sel_ind, s) for s in _sub_list}
            _cur_sub_val = st.session_state.get("selected_subcategory", "전체")
            if _cur_sub_val not in _sub_list:
                _cur_sub_val = "전체"
            _sel_sub = st.selectbox(
                "세부 분야",
                options=_sub_list,
                index=_sub_list.index(_cur_sub_val),
                format_func=lambda k: _sub_labels.get(k, k),
                key="_subcategory_sb",
            )
            if st.session_state.get("selected_subcategory") != _sel_sub:
                log_event("subcategory_select", {"subcategory": _sel_sub})
                # 서브카테고리 변경 시 기사 캐시 초기화
                st.session_state.pop("docs", None)
                st.session_state.pop("docs_others", None)
                st.session_state.pop("docs_fetched_at", None)
                st.session_state.pop("selected_id", None)
            st.session_state["selected_subcategory"] = _sel_sub
        else:
            st.session_state["selected_subcategory"] = "전체"

        # 선택된 산업 정보 표시
        st.markdown(f"**{_profile['icon']} {_profile['label']}**")
        st.caption(_profile["description"])
        if _profile["critical_variables"]:
            st.markdown("**📌 핵심 경제 변수**")
            for cv in _profile["critical_variables"]:
                st.markdown(f"- {cv}")

        # Fake Door 피드백 (일반 외 산업 선택 시)
        if _sel_ind != "일반":
            st.markdown("---")
            st.markdown("### 🚀 산업 맞춤 브리핑 준비 중")
            st.info(
                f"**{_profile['label']}** 맞춤 브리핑 기능을 준비하고 있습니다.\n\n"
                "아래 설문에 참여해 주시면 기능 개발에 큰 도움이 됩니다!"
            )
            st.markdown("---")
            st.markdown("**간단 피드백**")
            _fb_use = st.radio(
                "이 기능이 있다면 매일 사용하시겠습니까?",
                options=["예", "아니오", "모르겠음"],
                horizontal=True,
                key="fb_would_use",
            )
            _fb_text = st.text_area(
                "어떤 정보가 가장 필요한가요?",
                placeholder="예: 반도체 수출 규제 현황, 환율 영향 분석 등",
                key="fb_free_text",
            )
            if st.button("📩 피드백 제출", use_container_width=True, key="btn_feedback"):
                save_feedback(_sel_ind, _fb_use, _fb_text)
                log_event("feedback_submit", {"industry": _sel_ind, "would_use": _fb_use})
                st.session_state["feedback_done"] = True

            if st.session_state.get("feedback_done"):
                st.html("""
                <div style="
                    background: #ecfdf5;
                    border: 1px solid #10b981;
                    border-radius: 12px;
                    padding: 16px;
                    margin: 8px 0 0;
                    display: flex;
                    align-items: flex-start;
                    gap: 12px;
                ">
                  <span style="font-size:18px;line-height:1.4;flex-shrink:0">✅</span>
                  <div>
                    <div style="
                        font-size:13.5px;
                        font-weight:600;
                        color:#065f46;
                        margin-bottom:3px;
                    ">피드백이 성공적으로 제출되었습니다</div>
                    <div style="
                        font-size:12px;
                        color:#047857;
                        line-height:1.6;
                    ">
                      소중한 의견 감사합니다.<br>서비스 개선에 반영하겠습니다.
                    </div>
                  </div>
                </div>
                """)

        # ── 리포트 이메일 발송 ─────────────────────────
        st.markdown("---")
        st.markdown("### 📧 리포트 이메일 발송")
        try:
            from core.emailer import is_configured as _email_ok, send_report_email as _send_report
            _email_configured = _email_ok()
        except Exception:
            _email_configured = False

        if _email_configured:
            # 기본 수신자 표시
            try:
                _default_recip = st.secrets.get("email", {}).get("recipients", "")
            except Exception:
                _default_recip = ""
            if _default_recip:
                st.caption(f"기본 수신자: {_default_recip}")

            _extra_recip = st.text_input(
                "추가 수신자 (선택)",
                placeholder="추가@example.com (쉼표로 구분)",
                key="email_extra_recipients",
            )

            _docs_for_email   = st.session_state.get("docs", [])
            _doc_for_email    = st.session_state.get("last_doc")
            _detail_for_email = st.session_state.get("last_detail")
            _btn_disabled = not bool(_docs_for_email)

            if st.button(
                "📧 리포트 발송",
                use_container_width=True,
                disabled=_btn_disabled,
                help="정책브리핑 탭에서 기사를 로드하세요" if _btn_disabled else "현재 대시보드 리포트를 이메일로 발송",
                key="btn_send_report_email",
            ):
                with st.spinner("이메일 발송 중..."):
                    try:
                        _html = generate_report_html(_docs_for_email, _doc_for_email, _detail_for_email)
                        _profile_label = get_profile(_sel_ind).get("label", _sel_ind)
                        _subject = (
                            f"[{_date.today().strftime('%Y-%m-%d')}] "
                            f"{_profile_label} 경제신호 리포트"
                        )
                        _extra = [r.strip() for r in _extra_recip.split(",") if r.strip()] if _extra_recip else []
                        _ok = _send_report(_html, _subject, extra_recipients=_extra)
                        if _ok:
                            st.toast("✅ 이메일 발송 완료!")
                            log_event("report_email_sent", {"industry": _sel_ind})
                        else:
                            st.error("발송 실패 — 이메일 설정을 확인하세요")
                    except Exception as _e:
                        st.error(f"발송 오류: {_e}")
        else:
            st.caption("이메일 미설정 — secrets.toml [email] 섹션을 확인하세요")

        # ── 오늘의 활동 통계 ──────────────────────────
        st.divider()
        st.caption("📊 오늘의 활동")
        try:
            _today_stats = get_daily_summary()
            if _today_stats["total_events"] > 0:
                st.caption(
                    f"조회 {_today_stats['page_views']}회 · "
                    f"기사 클릭 {_today_stats['article_clicks']}회"
                )
            else:
                st.caption("아직 활동 기록이 없습니다")
        except Exception:
            st.caption("아직 활동 기록이 없습니다")

        # ── T-11: 이메일 구독 신청 ──────────────────────
        st.divider()
        st.subheader("📬 이메일 구독")
        with st.form("subscribe_form"):
            _sub_company = st.text_input("기업명")
            _sub_email = st.text_input("이메일")
            _sub_ind_options = [item["key"] for item in _industry_list if item["key"] != "일반"]
            _sub_industry = st.selectbox("관심 산업", _sub_ind_options)
            _sub_submit = st.form_submit_button("무료 구독 신청")
            if _sub_submit and _sub_email:
                try:
                    from core.subscription import add_subscriber
                    add_subscriber(_sub_company, _sub_email, _sub_industry)
                    st.success("구독 신청 완료!")
                    log_event("subscribe", {"company": _sub_company, "industry": _sub_industry})
                except Exception as _sub_err:
                    st.error(f"구독 신청 오류: {_sub_err}")
            elif _sub_submit and not _sub_email:
                st.warning("이메일을 입력해 주세요.")

        # ── 🔧 Debug Mode (QA Panel) ─────────────────────
        st.divider()
        _debug_mode = st.toggle("🔧 Debug Mode", key="debug_mode_toggle")
        if _debug_mode:
            st.markdown("### 🩺 System Health")
            try:
                from daily_live_qa import load_latest_qa_report, run_daily_qa, _health_emoji

                _qa_report = load_latest_qa_report()

                # QA 실행 버튼
                if st.button("▶ QA 실행", key="btn_run_qa", use_container_width=True):
                    with st.spinner("QA 파이프라인 실행 중..."):
                        _qa_report = run_daily_qa(verbose=False)
                    st.rerun()

                if _qa_report is None:
                    st.info("QA 리포트 없음 — 위 버튼으로 실행하세요.")
                else:
                    _health = _qa_report.get("overall_health", "Unknown")
                    _emoji = _qa_report.get("health_emoji", _health_emoji(_health))
                    _run_at = _qa_report.get("run_at", "—")
                    _summary = _qa_report.get("summary", {})

                    # Health Badge
                    _health_colors = {
                        "Green": ("#16a34a", "#f0fdf4"),
                        "Yellow": ("#ca8a04", "#fefce8"),
                        "Red": ("#dc2626", "#fef2f2"),
                        "Unknown": ("#6b7280", "#f9fafb"),
                    }
                    _fg, _bg = _health_colors.get(_health, ("#6b7280", "#f9fafb"))
                    st.html(f"""
                    <div style="
                        background:{_bg};
                        border:1.5px solid {_fg};
                        border-radius:8px;
                        padding:10px 14px;
                        margin:4px 0 8px;
                        text-align:center;
                    ">
                      <div style="font-size:22px">{_emoji}</div>
                      <div style="font-weight:700;color:{_fg};font-size:14px">{_health}</div>
                      <div style="font-size:10px;color:#6b7280;margin-top:2px">{_run_at}</div>
                    </div>
                    """)

                    # 요약 지표
                    _c1, _c2 = st.columns(2)
                    _c1.metric("🔴 CRITICAL", _summary.get("critical_count", 0))
                    _c2.metric("⚠️  WARNING", _summary.get("warning_count", 0))

                    # 체크별 상세 (expander)
                    _checks = _qa_report.get("checks", {})
                    _status_icons = {
                        "ok": "✅", "warning": "⚠️", "critical": "🔴", "error": "❌"
                    }
                    _check_labels = {
                        "source_ingestion_count": "소스 수집",
                        "junk_filtering_ratio": "정크 필터",
                        "zero_relevance_ratio": "Zero Relevance",
                        "ranking_stability": "Ranking 안정성",
                        "cache_ttl_status": "캐시 TTL",
                        "source_availability": "소스 가용성",
                    }
                    with st.expander("📋 체크 상세", expanded=False):
                        for _chk_key, _chk_data in _checks.items():
                            _st = _chk_data.get("status", "ok")
                            _ic = _status_icons.get(_st, "❓")
                            _label = _check_labels.get(_chk_key, _chk_key)
                            st.markdown(f"**{_ic} {_label}** — `{_st.upper()}`")
                            for _w in _chk_data.get("warnings", []):
                                st.caption(f"  └ {_w}")

            except Exception as _qa_err:
                st.error(f"QA 패널 로드 실패: {_qa_err}")

    # ── Hero Header (탭 바깥, 항상 표시) ────────────
    _render_dashboard_header()

    # ── ECOS 업데이트 버튼 ───────────────────────────
    _has_key = bool(_ecos_get_key())
    _, col_btn = st.columns([7, 1])
    with col_btn:
        if st.button("🔄 업데이트", key="btn_macro_refresh",
                     disabled=not _has_key,
                     help="ECOS API에서 최신 지표 수집" if _has_key
                          else "ECOS_API_KEY 환경변수 미설정",
                     use_container_width=True):
            with st.spinner("ECOS에서 데이터 수집 중..."):
                try:
                    _ecos_refresh()
                    log_event("macro_refresh")
                    # 업데이트 시 전체 산업 임팩트 스코어 일괄 저장
                    # → 산업별 탭을 직접 클릭하지 않아도 히스토리가 쌓임
                    try:
                        from core.impact_scorer import (
                            calculate_macro_impact_score,
                            update_and_get_score_delta,
                        )
                        from core.industry_config import INDUSTRY_PROFILES
                        _fresh_macro = _load_macro()
                        for _ind_k in INDUSTRY_PROFILES:
                            try:
                                _r = calculate_macro_impact_score(_fresh_macro, _ind_k)
                                update_and_get_score_delta(_ind_k, _r["total"])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    st.toast("✅ 거시지표 갱신 완료!")
                except Exception as _e:
                    st.error(f"갱신 실패: {_e}")
            st.rerun()

    # ── 오늘의 핵심 신호 카드 (Hero 바로 아래, 탭 위) ──
    _section_header("⚡ 오늘의 핵심 신호", "현재 경제 상황 요약 및 즉시 행동 가이드", "1")
    _render_today_signal(_sel_ind)

    # ── [T-22] Impact Strip (신호등 스트립) ──
    _render_impact_strip()

    # ══════════════════════════════════════════════════════════════
    # [T-08] 단일 스크롤 레이아웃 (탭 구조 대체)
    # ══════════════════════════════════════════════════════════════

    # ── [1] 오늘의 핵심 신호는 위에서 이미 렌더링됨 (_render_today_signal) ──

    # ── [2] 핵심 지표 KPI 카드 4종 ──────────────────────────────
    _section_header("📊 핵심 지표 KPI", "ECOS 한국은행 공식 거시경제 지표", "2")
    if _MACRO:
        _kpi_keys = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
        _kpi_items = [(k, _MACRO[k]) for k in _kpi_keys if k in _MACRO]
        if _kpi_items:
            _render_kpi_section_card(_kpi_items)
        # Macro validation warnings
        for _m_label, _m_data in _MACRO.items():
            _m_warn = _validate_macro_item(_m_label, _m_data)
            if _m_warn:
                st.warning(_m_warn)
    else:
        st.info("거시지표 데이터 없음 — ECOS API 키 설정 후 업데이트 버튼을 클릭하세요.")

    st.divider()

    # ── [2.4] 복합 리스크 지수 ─────────────────────────────────
    try:
        from core.risk_index import calculate_risk_index as _calc_risk
        from views.dashboard_main import render_risk_gauge as _render_rg
        _risk_data = _calc_risk(_MACRO, _sel_ind)
        _render_rg(_risk_data)
    except Exception:
        pass

    # ── [2.4b] 경쟁사 벤치마킹 ─────────────────────────────────
    _cp_bench = st.session_state.get("company_profile_v2") or st.session_state.get("company_profile")
    if _cp_bench and _MACRO:
        try:
            from views.benchmark_widget import render_benchmark_card as _render_bench
            _render_bench(
                company_profile=_cp_bench,
                industry_key=_sel_ind,
                macro_data=_MACRO,
            )
        except Exception:
            pass

    # ── [2.5] 시나리오 분석 ──────────────────────────────────
    with st.expander("🔮 시나리오 분석  🚧 개선 준비 중", expanded=False):
        try:
            from core.scenario_engine import SCENARIO_PRESETS, simulate_scenario

            _sc_options = list(SCENARIO_PRESETS.keys())
            _sc_labels = {k: f"{k} — {v['설명']}" for k, v in SCENARIO_PRESETS.items()}
            _sc_col1, _sc_col2 = st.columns([3, 1])
            with _sc_col1:
                _sc_selected = st.selectbox(
                    "시나리오 프리셋",
                    options=_sc_options,
                    format_func=lambda k: _sc_labels[k],
                    key="scenario_preset",
                )
            with _sc_col2:
                _sc_run = st.button("🚀 분석 실행", use_container_width=True, key="btn_scenario_run")

            if _sc_run and _MACRO:
                _sc_result = simulate_scenario(_MACRO, _sc_selected, _sel_ind)

                # 결과 metric 3개
                _sc_m1, _sc_m2, _sc_m3 = st.columns(3)
                with _sc_m1:
                    _sc_delta = _sc_result["impact_delta"]
                    st.metric(
                        label="영향도 변화",
                        value=f"{_sc_result['after_score']:+.1f}",
                        delta=f"{_sc_delta:+.1f}",
                    )
                with _sc_m2:
                    if _sc_result["affected_kpis"]:
                        _sc_kpi0 = _sc_result["affected_kpis"][0]
                        st.metric(
                            label=_sc_kpi0["kpi"],
                            value=f"{_sc_kpi0['after']:,.1f}",
                            delta=f"{_sc_kpi0['after'] - _sc_kpi0['before']:+,.1f}",
                        )
                    else:
                        st.metric(label="핵심 KPI", value="—")
                with _sc_m3:
                    _sc_actions = _sc_result["action_recommendations"]
                    st.metric(label="권고 액션", value=f"{len(_sc_actions)}건")
                    for _sc_act in _sc_actions:
                        st.caption(f"• {_sc_act}")

                # before/after KPI 시각화
                if _sc_result["affected_kpis"]:
                    import pandas as pd
                    _sc_chart_data = pd.DataFrame([
                        {"지표": kpi["kpi"], "Before": kpi["before"], "After": kpi["after"]}
                        for kpi in _sc_result["affected_kpis"]
                    ]).set_index("지표")
                    st.bar_chart(_sc_chart_data)
            elif _sc_run and not _MACRO:
                st.warning("거시지표 데이터가 없습니다. ECOS 업데이트를 먼저 실행하세요.")
        except Exception as _sc_err:
            st.error(f"시나리오 분석 오류: {_sc_err}")

    # ── [2.6] 글로벌 시장 추천 ───────────────────────────────
    with st.expander("🌏 글로벌 시장 추천  🚧 개선 준비 중", expanded=False):
        try:
            from core.market_recommender import recommend_markets as _recommend_markets

            if st.button("🚀 추천 받기", use_container_width=True, key="btn_market_recommend"):
                with st.spinner("유망 시장 분석 중..."):
                    _mr_results = _recommend_markets(_sel_ind, _MACRO)

                if _mr_results:
                    _mr_cols = st.columns(len(_mr_results))
                    for _mr_col, _mr in zip(_mr_cols, _mr_results):
                        with _mr_col:
                            _mr_fta_badge = "✅ FTA" if _mr["fta"] else "—"
                            st.markdown(f"### {_mr['country']}")
                            st.progress(min(_mr["score"], 100), text=f"종합 점수: {_mr['score']}점")
                            st.metric(label="수출 성장률", value=_mr["growth_rate"])
                            st.caption(f"교역 규모: {_mr['trade_value']}")
                            st.caption(f"FTA: {_mr_fta_badge}")
                            st.info(_mr["reason"])
                else:
                    st.warning("추천 가능한 시장 데이터가 없습니다.")
        except Exception as _mr_err:
            st.error(f"시장 추천 오류: {_mr_err}")

    st.divider()

    # ── [3] 산업별 핵심 변수 카드 ────────────────────────────
    _section_header("🔬 산업별 핵심 변수", "선택 산업의 경제 민감 변수 실시간 모니터링", "3")
    _render_industry_variable_card(_sel_ind, st.session_state.get("docs", []))

    st.divider()

    # ── [3.5] 전략 옵션 (Decision Engine) ──────────────────
    _today_sig = generate_today_signal(_MACRO, _sel_ind)
    _decision_opts = generate_decision_options(_MACRO, _sel_ind, _today_sig)
    if _decision_opts:
        _section_header("🎯 전략 옵션", "현재 경제 신호 기반 — 기업이 취할 수 있는 전략 3가지", "4")
        _urg_style = {
            "즉시":   ("🔴", "#fef2f2", "#dc2626", "#991b1b"),
            "이번 주": ("🟠", "#fff7ed", "#f97316", "#9a3412"),
            "이번 달": ("🟢", "#f0fdf4", "#22c55e", "#166534"),
        }
        _diff_icon = {"낮음": "●○○", "중간": "●●○", "높음": "●●●"}
        _impact_icon = {"낮음": "▪", "중간": "▪▪", "높음": "▪▪▪"}

        _opt_cols = st.columns(3)
        for _opt, _col in zip(_decision_opts, _opt_cols):
            with _col:
                _u_emoji, _u_bg, _u_border, _u_text = _urg_style.get(
                    _opt["urgency"], ("🟢", "#f0fdf4", "#22c55e", "#166534")
                )
                _d_dots = _diff_icon.get(_opt["difficulty"], "●○○")
                _i_dots = _impact_icon.get(_opt["impact"], "▪")
                st.html(f"""
                <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;
                            padding:20px;height:100%;font-family:'Inter',sans-serif;
                            border-top:4px solid {_u_border}">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                    <span style="font-size:13px;font-weight:800;color:#5B5FEE">옵션 {_opt['option']}</span>
                    <span style="background:{_u_bg};color:{_u_text};font-size:11px;font-weight:700;
                                  padding:3px 10px;border-radius:12px;border:1px solid {_u_border}">
                      {_u_emoji} {_opt['urgency']}
                    </span>
                  </div>
                  <div style="font-size:15px;font-weight:700;color:#1e293b;margin-bottom:8px;
                              line-height:1.4">{_opt['title']}</div>
                  <div style="font-size:12.5px;color:#475569;line-height:1.6;margin-bottom:14px">
                    {_opt['rationale']}
                  </div>
                  <div style="display:flex;gap:16px;font-size:11px;color:#64748b">
                    <span>난이도 <b style="color:#334155">{_d_dots}</b></span>
                    <span>임팩트 <b style="color:#5B5FEE">{_i_dots}</b></span>
                  </div>
                </div>
                """)

    st.divider()

    # ── [4] 주요 기사 목록 (임팩트 스코어 내림차순) ──────────
    _section_header("📰 주요 기사 목록", "KDI 나라경제 + 뉴스 RSS — 임팩트 스코어 내림차순", "5")

    # 기사 자동 수집 (session state 초기화)
    st.session_state.setdefault("docs", [])
    st.session_state.setdefault("selected_id", None)
    st.session_state.setdefault("last_doc", None)
    st.session_state.setdefault("last_detail", None)
    st.session_state.setdefault("docs_fetched_at", "")

    _cur_ind = st.session_state.get("selected_industry", "일반")
    _cur_sub = st.session_state.get("selected_subcategory", "전체")  # V17.8
    if not st.session_state.docs:
        with st.spinner("KDI 나라경제 목록 자동 수집 중..."):
            try:
                _raw = fetch_list(_KDI_URL, 20)
                # T-07: 멀티 소스 통합 (뉴스 RSS)
                try:
                    from core.extra_sources import fetch_all_sources
                    _raw, _src_stats = fetch_all_sources(_raw, kotra_max=5, industry_key=_cur_ind)
                    print(f"[extra_sources] source_stats: {_src_stats}")
                except Exception as _extra_e:
                    print(f"[extra_sources] 통합 실패, KDI만 사용: {_extra_e}")
                _rel, _oth = _filter_relevant_docs(_raw, _cur_ind, _cur_sub)
                st.session_state.docs = _rel if _rel else _raw
                st.session_state.docs_others = _oth if _rel else []
                st.session_state.docs_fetched_at = _dt.now().strftime("%Y-%m-%d %H:%M")
            except Exception as _e:
                st.error(f"자동 수집 오류: {_e}")

    # 새로고침 + 필터
    _scroll_col1, _scroll_col2 = st.columns([3, 1])
    with _scroll_col1:
        _scroll_kw = st.text_input("키워드 검색", placeholder="제목 내 검색", key="scroll_kw_search")
    with _scroll_col2:
        _scroll_top_n = st.number_input(
            "목록 수", min_value=5, max_value=50, value=20, step=5,
            key="scroll_top_n",
        )
        if st.button("🔄 새로 고침", type="primary", use_container_width=True, key="scroll_btn_load"):
            with st.spinner("목록 수집 중..."):
                try:
                    _raw = fetch_list(_KDI_URL, int(_scroll_top_n))
                    # T-07: 멀티 소스 통합 (뉴스 RSS)
                    try:
                        from core.extra_sources import fetch_all_sources
                        _raw, _src_stats = fetch_all_sources(_raw, kotra_max=5, industry_key=_cur_ind)
                        print(f"[extra_sources] source_stats: {_src_stats}")
                    except Exception as _extra_e:
                        print(f"[extra_sources] 통합 실패, KDI만 사용: {_extra_e}")
                    _rel, _oth = _filter_relevant_docs(_raw, _cur_ind, _cur_sub)
                    st.session_state.docs = _rel if _rel else _raw
                    st.session_state.docs_others = _oth if _rel else []
                    st.session_state.docs_fetched_at = _dt.now().strftime("%Y-%m-%d %H:%M")
                    st.session_state.selected_id = None
                    st.session_state.last_doc = None
                    st.session_state.last_detail = None
                    if _rel:
                        st.toast(f"✅ {len(_rel)}건 관련 기사 필터링 완료 (전체 {len(_raw)}건 중)")
                except Exception as e:
                    st.error(f"오류: {e}")

    docs: list = st.session_state.docs

    if docs:
        # 임팩트 스코어 일괄 산출
        _scored_docs = score_articles(docs, _cur_ind, _MACRO, subcategory=_cur_sub)

        # ── V17.7 Heuristic Gate: 저품질 기사 Top 3 완전 배제 ──
        # 배제 조건: no_fetch=True | _google_news=True | 코트라 기사 중 URL 미해독
        def _is_low_quality(d: dict) -> bool:
            if d.get("no_fetch") or d.get("_google_news"):
                return True
            # 코트라 기사인데 URL이 google.com/news 계열이면 미해독
            if d.get("source") == "코트라":
                _url = d.get("url", "")
                if "news.google.com" in _url or "google.com/rss" in _url:
                    return True
            return False

        _hi_docs = [d for d in _scored_docs if not _is_low_quality(d)]  # Top 3 후보
        _lo_docs = [d for d in _scored_docs if _is_low_quality(d)]      # 하위 배치

        # V17.8: 산업직접연관 + 서브카테고리매칭 + full_body 우선 정렬
        # (_ind, _sub, _fb, impact_score) 내림차순
        def _sort_key(d: dict):
            _ind = 1 if d.get("_ind_score", 0) >= 2 else 0   # 산업 직접 키워드 매칭
            _sub = 1 if d.get("_sub_score", 0) >= 1 else 0    # 서브카테고리 매칭
            _fb = 1 if d.get("analysis_source") == "full_body" else 0
            return (_ind, _sub, _fb, d.get("impact_score", 1))

        _hi_docs = sorted(_hi_docs, key=_sort_key, reverse=True)
        _lo_docs = sorted(_lo_docs, key=lambda d: d.get("impact_score", 1), reverse=True)

        # V17.8.1: 서브카테고리 Top N 게이트
        # subcategory != "전체"일 때, sub_score=0 기사는 Top 5 밖으로 밀어냄
        # → sub 매칭 기사가 반드시 상위 노출, 미매칭은 후속 배치
        if _cur_sub != "전체" and _hi_docs:
            _sub_match = [d for d in _hi_docs if d.get("_sub_score", 0) >= 1]
            _sub_miss  = [d for d in _hi_docs if d.get("_sub_score", 0) < 1]
            _hi_docs = _sub_match + _sub_miss  # 매칭 우선, 미매칭 후속

        # 고품질 우선 + 저품질 후속 배치
        _scored_docs = _hi_docs + _lo_docs

        # 키워드 필터 적용
        if _scroll_kw:
            _scored_docs = [d for d in _scored_docs if _scroll_kw in d.get("title", "")]

        # ── 출처 필터 ────────────────────────────────────────
        _available_sources = sorted({d.get("source", "KDI") for d in _scored_docs})
        _source_options = ["전체"] + _available_sources
        _sel_source = st.selectbox(
            "출처 필터", _source_options,
            key="source_filter", label_visibility="collapsed",
        )
        if _sel_source != "전체":
            _scored_docs = [d for d in _scored_docs if d.get("source", "KDI") == _sel_source]

        _fetched_at = st.session_state.get("docs_fetched_at", "")
        if _fetched_at:
            _hi_cnt = len([d for d in _scored_docs if not _is_low_quality(d)])
            st.caption(f"기사 {len(_scored_docs)}건 (본문확보 {_hi_cnt}건) | 기준: {_fetched_at}(KST) | 임팩트 스코어 높은 순")

        # ── T-23: 임팩트 Top 3 + 더보기 구조 ──────────────
        _TOP_N = 3
        _show_all_key = "show_all_articles"
        st.session_state.setdefault(_show_all_key, False)
        _show_all = st.session_state[_show_all_key]

        _score_badge_cfg = {
            5: ("🔥 HOT", "#fef2f2", "#dc2626"),
            4: ("⭐ 주요", "#fffbeb", "#f59e0b"),
        }
        _accent_colors = {5: "#dc2626", 4: "#f59e0b", 3: "#3b82f6", 2: "#94a3b8", 1: "#cbd5e1"}

        _visible_docs = _scored_docs if _show_all else _scored_docs[:_TOP_N]

        # 출처 배지 맵 (공통)
        _SRC_BADGE_MAP = {
            "KDI": ("background:#dbeafe;color:#1e40af;border:1px solid #93c5fd", "KDI"),
            "연합뉴스경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "연합뉴스"),
            "매일경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "매일경제"),
            "한국경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "한국경제"),
            "산업부": ("background:#fff7ed;color:#9a3412;border:1px solid #fdba74", "산업부"),
        }

        for _art_idx, _art in enumerate(_visible_docs, start=1):
            _art_score = _art.get("impact_score", 1)
            _art_stars = "★" * _art_score
            _art_title = _art.get("title", "제목 없음")
            _art_yyyymm = _art.get("issue_yyyymm", "")
            _art_date = f"[{_art_yyyymm[:4]}.{_art_yyyymm[4:]}] " if len(_art_yyyymm) == 6 else ""
            _accent = _accent_colors.get(_art_score, "#cbd5e1")

            # 배지 (5점: HOT, 4점: 주요)
            _sb_label, _sb_bg, _sb_color = _score_badge_cfg.get(_art_score, ("", "", ""))
            if _sb_label:
                st.html(
                    f'<div style="display:inline-block;background:{_sb_bg};'
                    f'color:{_sb_color};font-size:11px;font-weight:700;'
                    f'padding:2px 10px;border-radius:10px;margin-bottom:4px;'
                    f'border:1px solid {_sb_color}">{_sb_label} 임팩트 {_art_score}점</div>'
                )

            # T-09: 이메일 앵커 링크로 접근 시 해당 기사 자동 펼침
            _is_target = str(_art_idx) == str(_target_article_id) if _target_article_id else False
            # Top 3은 기본 펼침
            _auto_expand = _is_target or (_art_idx <= _TOP_N and not _show_all)
            if _is_target:
                try:
                    log_event("article_click", {"doc_id": _art.get("doc_id", ""), "title": _art_title[:50], "source": "email"})
                except Exception:
                    pass

            # 출처 태그 (expander 제목에 표시)
            _art_source = _art.get("source", "KDI")
            _src_tag = f" [{_art_source}]" if _art_source and _art_source != "KDI" else ""
            with st.expander(f"[{_art_stars}] {_art_date}{_art_title}{_src_tag}", expanded=_auto_expand):
                # 컬러 accent bar + 출처 배지
                _badge_style, _badge_text = _SRC_BADGE_MAP.get(
                    _art_source, ("background:#f1f5f9;color:#475569;border:1px solid #e2e8f0", _art_source)
                )
                st.html(
                    f'<div style="height:3px;background:{_accent};border-radius:2px;margin-bottom:8px"></div>'
                    f'<span style="{_badge_style};font-size:10px;font-weight:700;'
                    f'padding:2px 8px;border-radius:8px">{_badge_text}</span>'
                )

                # 기사 상세 로드
                with st.spinner("본문 수집 중..."):
                    try:
                        _art_detail = fetch_detail(
                            _art["doc_id"], _art["url"], _art["title"],
                            industry_key=_cur_ind,
                        )
                        # LLM 재요약 시도
                        if (
                            _art_detail.get("parse_status") == "success"
                            and _art_detail.get("summary_source") == "rule"
                            and _art_detail.get("body_text")
                        ):
                            try:
                                from core.summarizer import _get_llm_key, summarize_3line as _re_summarize
                                if _get_llm_key():
                                    _new_sum, _new_src = _re_summarize(
                                        _art_detail["body_text"],
                                        title=_art.get("title", ""),
                                        industry_key=_cur_ind,
                                    )
                                    if _new_src == "groq":
                                        _art_detail = {**_art_detail, "summary_3lines": _new_sum, "summary_source": "groq"}
                            except Exception:
                                pass
                    except Exception as _fetch_err:
                        st.error(f"본문 수집 오류: {_fetch_err}")
                        _art_detail = None

                if _art_detail:
                    _sum_data = _art_detail.get("summary_3lines")
                    import re as _re_mod

                    # Markdown **bold** → HTML <b>bold</b> 헬퍼
                    def _md_bold(t: str) -> str:
                        return _re_mod.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t) if t else t

                    # V6: 소스 배지
                    _summary_source = _art_detail.get("summary_source", "")
                    if _summary_source:
                        _src_display = {
                            "groq": ("AI 분석", "#22C55E"), "cache": ("캐시", "#3B82F6"),
                            "industry_fallback": ("산업 분석", "#F59E0B"),
                            "smart_fallback": ("자동 분석", "#F97316"),
                            "snippet_fallback": ("스니펫 분석", "#F97316"),
                            "snippet_llm": ("AI 분석", "#22C55E"),
                            "rule_enhanced": ("규칙 분석", "#EF4444"),
                            "title_guard": ("제목 분석", "#EF4444"),
                            "minimal_fallback": ("원문 미확보", "#9CA3AF"),
                        }
                        _src_label, _src_color = _src_display.get(_summary_source, ("분석", "#6B7280"))
                        st.html(
                            f'<span style="font-size:10px;padding:2px 6px;border-radius:4px;'
                            f'background:{_src_color};color:white;font-weight:600">'
                            f'🔍 {_src_label}</span>'
                        )

                    # headline 표시
                    _headline = _sum_data.get("headline", "") if isinstance(_sum_data, dict) else ""
                    if _headline:
                        st.markdown(f"**⭐ {_md_bold(_headline)}**")

                    # 4-frame 요약 (impact/risk/opportunity/action)
                    if isinstance(_sum_data, dict) and "impact" in _sum_data:
                        _frame_items = [
                            ("📊 Impact", _sum_data.get("impact", ""), "#3B82F6"),
                            ("📉 Risk", _sum_data.get("risk", ""), "#EF4444"),
                            ("💡 Opportunity", _sum_data.get("opportunity", ""), "#22C55E"),
                            ("✅ Action", _sum_data.get("action", ""), "#5B5FEE"),
                        ]
                        _frame_html = ""
                        for _fl, _ft, _fc in _frame_items:
                            if _ft:
                                _ft = _md_bold(_ft)
                                # Action 필드: bullet 포인트 분리 처리
                                if "Action" in _fl and ("•" in _ft or "· " in _ft):
                                    _bullets = [b.strip() for b in _re_mod.split(r'[•·]', _ft) if b.strip()]
                                    _bullet_html = "".join(
                                        f'<div style="font-size:13px;color:#334155;padding:2px 0">'
                                        f'• {b}</div>' for b in _bullets
                                    )
                                    _frame_html += (
                                        f'<div style="padding:8px 12px;border-left:3px solid {_fc};'
                                        f'margin-bottom:6px;background:rgba(0,0,0,0.02);border-radius:0 8px 8px 0">'
                                        f'<span style="font-size:11px;font-weight:700;color:{_fc}">{_fl}</span>'
                                        f'{_bullet_html}</div>'
                                    )
                                else:
                                    _frame_html += (
                                        f'<div style="padding:8px 12px;border-left:3px solid {_fc};'
                                        f'margin-bottom:6px;background:rgba(0,0,0,0.02);border-radius:0 8px 8px 0">'
                                        f'<span style="font-size:11px;font-weight:700;color:{_fc}">{_fl}</span>'
                                        f'<div style="font-size:13px;color:#334155;margin-top:2px;'
                                        f'line-height:1.6">{_ft}</div></div>'
                                    )
                        if _frame_html:
                            st.html(f'<div style="font-family:Inter,sans-serif">{_frame_html}</div>')

                        # ❓ 경영진 질문 + 📋 점검 항목 (LLM 응답의 questions/checklist 필드)
                        _questions = _sum_data.get("questions", "") if isinstance(_sum_data, dict) else ""
                        _checklist_v4 = _sum_data.get("checklist", "") if isinstance(_sum_data, dict) else ""

                        # fallback: questions/checklist가 없으면 industry_config에서 생성
                        def _fill_tpl(tpl: str, topic: str) -> str:
                            r = tpl.replace("{context}", topic).replace("{" + topic + "}", topic)
                            return r

                        if not _questions or not _questions.strip():
                            try:
                                _fb_profile = get_profile(_cur_ind)
                                _fb_qf = _fb_profile.get("questions_frame", [])
                                _fb_topic = _art.get("title", "")[:20]
                                if _fb_qf:
                                    _questions = "\n".join(f"• {_fill_tpl(q, _fb_topic)[:70]}" for q in _fb_qf[:3])
                            except Exception:
                                pass

                        if not _checklist_v4 or not _checklist_v4.strip():
                            try:
                                _fb_profile2 = get_profile(_cur_ind)
                                _fb_clf = _fb_profile2.get("checklist_frame", [])
                                _fb_topic2 = _art.get("title", "")[:20]
                                if _fb_clf:
                                    _checklist_v4 = "\n".join(f"• {_fill_tpl(c, _fb_topic2)[:55]}" for c in _fb_clf[:3])
                            except Exception:
                                pass

                        if _questions:
                            _q_html = _md_bold(_questions.replace("\n", "<br>"))
                            st.markdown(
                                f'<div style="background:#F0F7FF;border-left:3px solid #3B82F6;padding:8px 12px;margin:4px 0;border-radius:4px;font-size:0.85em">'
                                f'<strong style="color:#1E40AF">❓ 경영진 질문</strong><br>{_q_html}</div>',
                                unsafe_allow_html=True,
                            )

                        if _checklist_v4:
                            _cl_html = _md_bold(_checklist_v4.replace("\n", "<br>"))
                            st.markdown(
                                f'<div style="background:#F0FFF4;border-left:3px solid #22C55E;padding:8px 12px;margin:4px 0;border-radius:4px;font-size:0.85em">'
                                f'<strong style="color:#166534">📋 점검 항목</strong><br>{_cl_html}</div>',
                                unsafe_allow_html=True,
                            )
                    else:
                        # 기존 3줄 요약
                        _pstatus = _art_detail.get("parse_status", "fail")
                        if _pstatus == "success" and _sum_data:
                            _render_summary_3lines(
                                _sum_data,
                                source=_art_detail.get("summary_source", ""),
                            )
                        else:
                            st.warning(f"⚠️ 요약 생성 불가: {_art_detail.get('fail_reason', '수집 실패')}")

                    # 원문 링크
                    if _art.get("url"):
                        st.markdown(f"🔗 [원문 보기]({_art['url']})")

                    # 선택 문서로 저장 (리포트 다운로드용)
                    st.session_state.last_doc = _art
                    st.session_state.last_detail = _art_detail

        # "더보기" / "접기" 토글 버튼
        _remaining = len(_scored_docs) - _TOP_N
        if _remaining > 0 and not _show_all:
            if st.button(f"📄 + {_remaining}개 기사 더 보기", use_container_width=True, key="btn_show_more_articles"):
                st.session_state[_show_all_key] = True
                st.rerun()
        elif _show_all and _remaining > 0:
            if st.button("🔼 Top 3만 보기", use_container_width=True, key="btn_show_less_articles"):
                st.session_state[_show_all_key] = False
                st.rerun()

        # 기타 기사 (관련성 낮음)
        _others = st.session_state.get("docs_others", [])
        if _others:
            _filtered_total = len(st.session_state.get("docs", [])) + len(_others)
            with st.expander(f"기타 기사 {len(_others)}건 (관련성 낮음) — {len(_others)}건 필터링됨"):
                for _od in _others:
                    st.caption(f"📄 {_od['title'][:50]}")
    elif not st.session_state.docs:
        st.info("목록을 불러오는 중입니다...")

    _render_content_history()
    _render_download_section(st.session_state.docs)

    # ── 리포트 이메일 발송 (다운로드 섹션 바로 아래) ──────────────
    try:
        from core.emailer import is_configured as _email_ok2, send_report_email as _send_report2
        _email_configured2 = _email_ok2()
    except Exception:
        _email_configured2 = False

    if _email_configured2:
        _docs_for_email2   = st.session_state.get("docs", [])
        _doc_for_email2    = st.session_state.get("last_doc")
        _detail_for_email2 = st.session_state.get("last_detail")
        _btn_disabled2 = not bool(_docs_for_email2)
        st.html("""
        <div style="margin-top:4px;margin-bottom:4px;font-family:'Inter',sans-serif;
                    font-size:12px;color:#64748b">
          📧 리포트를 이메일로 바로 받아보세요
        </div>
        """)
        if st.button(
            "📧 이메일로 리포트 발송",
            use_container_width=True,
            disabled=_btn_disabled2,
            help="정책브리핑 탭에서 기사를 로드하세요" if _btn_disabled2 else "현재 대시보드 리포트를 이메일로 발송",
            key="btn_send_report_email_main",
        ):
            with st.spinner("이메일 발송 중..."):
                try:
                    from core.emailer import send_report_email as _sre
                    _html2 = generate_report_html(_docs_for_email2, _doc_for_email2, _detail_for_email2)
                    _profile_label2 = get_profile(_sel_ind).get("label", _sel_ind)
                    _subject2 = (
                        f"[{_date.today().strftime('%Y-%m-%d')}] "
                        f"{_profile_label2} 경제신호 리포트"
                    )
                    _ok2 = _sre(_html2, _subject2)
                    if _ok2:
                        st.toast("✅ 이메일 발송 완료!")
                        log_event("report_email_sent", {"industry": _sel_ind})
                    else:
                        st.error("발송 실패 — 이메일 설정을 확인하세요")
                except Exception as _e2:
                    st.error(f"발송 오류: {_e2}")

    # ── 분석 품질 대시보드 (V11) ─────────────────────────────────
    try:
        from core.summarizer import get_quality_metrics as _get_qm
        _qm = _get_qm()
        if _qm.get("total_calls", 0) > 0:
            with st.expander("📊 분석 품질 대시보드", expanded=False):
                _q1, _q2, _q3, _q4 = st.columns(4)
                _q1.metric("AI 분석률", f"{_qm.get('groq_rate', 0)}%")
                _q2.metric("평균 품질", f"{_qm.get('avg_quality', 0)}/100")
                _q3.metric("총 분석", f"{_qm['total_calls']}건")
                _q4.metric("폴백률", f"{_qm.get('fallback_rate', 0)}%")
                _ind_avg = _qm.get("industry_avg", {})
                if _ind_avg:
                    st.caption("산업별 평균 품질: " + " | ".join(f"{k}: {v}점" for k, v in _ind_avg.items()))
    except Exception:
        pass

    # ── 성능 병목 리포트 (V13) ─────────────────────────────────
    try:
        from core.fetcher import get_fetch_perf_stats as _get_fps
        from core.article_cache import get_cache as _get_ac
        _ps = _get_fps()
        _ac_stats = _get_ac().stats()
        if _ps.get("calls", 0) > 0:
            with st.expander("⏱️ 성능 병목 리포트 (V13)", expanded=False):
                _p1, _p2, _p3, _p4 = st.columns(4)
                _p1.metric("KDI fetch 합계", f"{_ps['fetch_total_s']:.1f}s")
                _p2.metric("본문 추출 합계", f"{_ps['extract_total_s']:.1f}s")
                _p3.metric("LLM 합계", f"{_ps['summarize_total_s']:.1f}s")
                _p4.metric("캐시 히트율", f"{_ps['cache_hit_rate']}%")
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════════
    # [6] ⚙️ 워치리스트 설정
    # ══════════════════════════════════════════════════════════════
    st.divider()
    _section_header("⚙️ 워치리스트 설정", "임계값 초과 시 이메일 알림", "6")
    st.caption("거시지표가 설정한 임계값을 초과하면 이메일 알림을 받습니다.")

    try:
        from core.watchlist import get_items, add_item, remove_item

        _WL_INDICATORS = [
            "환율(원/$)", "소비자물가(CPI)", "수출증가율",
            "기준금리", "원/100엔 환율", "수출물가지수", "수입물가지수",
        ]
        _WL_CONDITIONS = {"이상 (above)": "above", "이하 (below)": "below", "변동률 초과 (%)": "change_pct"}

        with st.expander("➕ 새 워치리스트 항목 추가", expanded=False):
            with st.form("watchlist_add_form"):
                _wl_cols = st.columns([2, 2, 1.5])
                with _wl_cols[0]:
                    _wl_ind = st.selectbox("지표", _WL_INDICATORS, key="wl_indicator")
                with _wl_cols[1]:
                    _wl_cond_label = st.selectbox("조건", list(_WL_CONDITIONS.keys()), key="wl_condition")
                with _wl_cols[2]:
                    _wl_thr = st.number_input("임계값", value=1400.0, step=10.0, key="wl_threshold")
                _wl_submit = st.form_submit_button("추가", use_container_width=True)
                if _wl_submit:
                    _wl_cond = _WL_CONDITIONS[_wl_cond_label]
                    add_item(
                        indicator=_wl_ind,
                        condition=_wl_cond,
                        threshold=_wl_thr,
                        industry_keys=[_sel_ind] if _sel_ind != "일반" else [],
                        notify_email=True,
                    )
                    st.success(f"✅ 추가 완료: {_wl_ind} {_wl_cond_label.split(' ')[0]} {_wl_thr}")
                    st.rerun()

        _wl_items = get_items()
        if _wl_items:
            import pandas as pd
            _wl_cond_kr = {"above": "이상", "below": "이하", "change_pct": "변동률%"}
            _wl_rows = []
            for _it in _wl_items:
                _wl_rows.append({
                    "지표": _it.get("indicator", ""),
                    "조건": _wl_cond_kr.get(_it.get("condition", ""), _it.get("condition", "")),
                    "임계값": _it.get("threshold", 0),
                    "관련 산업": ", ".join(_it.get("industry_keys", [])) or "전체",
                    "이메일": "✅" if _it.get("notify_email") else "❌",
                    "마지막 발동": _it.get("last_triggered") or "—",
                })
            # 워치리스트 테이블 — custom HTML (교대 배경색 + threshold 강조)
            _wl_header = """
            <tr style="background:#5B5FEE;">
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:left;border:none">지표</th>
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:left;border:none">조건</th>
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:left;border:none">임계값 ⚡</th>
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:left;border:none">산업</th>
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:center;border:none">📧</th>
              <th style="padding:10px 14px;font-size:12px;font-weight:700;color:#fff;text-align:left;border:none">마지막 발동</th>
            </tr>"""
            _wl_body = ""
            for _ri, _row in enumerate(_wl_rows):
                _bg = "#F4F4FF" if _ri % 2 == 0 else "#ffffff"
                try:
                    _thr_fmt = f"{float(_row['임계값']):,.1f}"
                except Exception:
                    _thr_fmt = str(_row["임계값"])
                _email_cell = '<span style="color:#22c55e;font-size:15px">✔</span>' if _row["이메일"] == "✅" else '<span style="color:#94a3b8">—</span>'
                _last = _row["마지막 발동"] if _row["마지막 발동"] != "—" else '<span style="color:#94a3b8">—</span>'
                _wl_body += f"""
                <tr style="background:{_bg};">
                  <td style="padding:11px 14px;font-size:13px;font-weight:600;color:#1e293b;border-top:1px solid #e2e8f0">{_row['지표']}</td>
                  <td style="padding:11px 14px;font-size:13px;color:#475569;border-top:1px solid #e2e8f0">{_row['조건']}</td>
                  <td style="padding:11px 14px;font-size:13px;font-weight:700;color:#5B5FEE;border-top:1px solid #e2e8f0">{_thr_fmt}</td>
                  <td style="padding:11px 14px;font-size:13px;color:#475569;border-top:1px solid #e2e8f0">{_row['관련 산업']}</td>
                  <td style="padding:11px 14px;text-align:center;border-top:1px solid #e2e8f0">{_email_cell}</td>
                  <td style="padding:11px 14px;font-size:12px;color:#64748b;border-top:1px solid #e2e8f0">{_last}</td>
                </tr>"""
            st.html(f"""
            <div style="border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;
                        font-family:'Inter',sans-serif;margin-bottom:8px">
              <table style="width:100%;border-collapse:collapse">
                <thead>{_wl_header}</thead>
                <tbody>{_wl_body}</tbody>
              </table>
            </div>
            """)

            with st.expander("🗑️ 항목 삭제"):
                _wl_del_options = {
                    f"{it['indicator']} {_wl_cond_kr.get(it.get('condition',''), '')} {it.get('threshold', '')}": it["id"]
                    for it in _wl_items
                }
                _wl_del_sel = st.selectbox("삭제할 항목", list(_wl_del_options.keys()), key="wl_delete_sel")
                if st.button("삭제", key="wl_delete_btn"):
                    remove_item(_wl_del_options[_wl_del_sel])
                    st.success("삭제 완료")
                    st.rerun()
        else:
            st.info("등록된 워치리스트 항목이 없습니다. 위에서 추가해주세요.")

    except Exception as _wl_err:
        st.warning(f"워치리스트 로드 실패: {_wl_err}")

    # ══════════════════════════════════════════════════════════════
    # [T-08] 탭 구조 → 단일 스크롤 변경으로 주석 처리 (원본 코드 보존)
    # ══════════════════════════════════════════════════════════════
    # [T-08] # ── 메인 탭 ─────────────────────────────────────
    # [T-08] tab1, tab2, tab3 = st.tabs(["📊 경제신호", "📰 정책브리핑", "📥 리포트"])
    # [T-08]
    # [T-08] # ══ TAB 1: 경제신호 ══════════════════════════════
    # [T-08] with tab1:
    # [T-08] # 신호 사전 계산 (여러 렌더링 함수에서 공유)
    # [T-08] try:
    # [T-08] _signals = detect_macro_signals(_MACRO, _sel_ind) if _MACRO else []
    # [T-08] except Exception:
    # [T-08] _signals = []
    # [T-08]
    # [T-08] # [Item 4] 임계값 알림 — 최상단, 포커스 배너 위
    # [T-08] if _MACRO:
    # [T-08] _render_threshold_alerts(_MACRO, _sel_ind)
    # [T-08]
    # [T-08] # 0. 산업 포커스 배너 (선택 산업 + 민감도 태그)
    # [T-08] _render_industry_focus(_sel_ind)
    # [T-08]
    # [T-08] # 0-0. 산업별 핵심 변수 카드 (일반 외 산업 선택 시)
    # [T-08] _render_industry_variable_card(_sel_ind, st.session_state.get("docs", []))
    # [T-08] st.divider()
    # [T-08]
    # [T-08] # 0-1. 산업 임팩트 스코어 배너 (거시지표 trend 기반 자동 산출)
    # [T-08] if _MACRO:
    # [T-08] _render_impact_score_banner(_MACRO, _sel_ind)
    # [T-08]
    # [T-08] # [Item 3] Signal-to-Action 브리핑 카드 — 임팩트 배너 직후
    # [T-08] if _MACRO and _signals:
    # [T-08] _render_action_briefing(_MACRO, _sel_ind, _signals)
    # [T-08]
    # [T-08] # 1. 오늘의 거시경제 신호 + 3줄 브리핑
    # [T-08] _render_daily_signal_summary(_MACRO, _sel_ind)
    # [T-08] st.divider()
    # [T-08]
    # [T-08] # 2. 산업별 영향 요약 (3열 카드)
    # [T-08] _render_industry_impact_summary(_signals, _sel_ind)
    # [T-08] st.divider()
    # [T-08]
    # [T-08] # 3. 전략적 시사점 (expander × 3)
    # [T-08] _render_strategic_insights(_MACRO, _sel_ind)
    # [T-08] st.divider()
    # [T-08]
    # [T-08] # 4. 핵심 거시경제 지표 KPI (v2 — 큰 숫자 + 색상 테두리 + 출처)
    # [T-08] st.html("""
    # [T-08] <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;margin-top:4px">
    # [T-08] <span style="background:#5B5FEE;color:white;min-width:26px;height:26px;
    # [T-08] border-radius:50%;display:inline-flex;align-items:center;
    # [T-08] justify-content:center;font-size:12px;font-weight:800;flex-shrink:0">4</span>
    # [T-08] <span style="font-size:14px;font-weight:800;color:#1E1B4B">핵심 지표</span>
    # [T-08] <span style="font-size:11px;color:#5B5FEE;font-weight:600;
    # [T-08] background:#EAEBFF;padding:2px 10px;border-radius:20px">
    # [T-08] Key Economic Indicators
    # [T-08] </span>
    # [T-08] </div>
    # [T-08] """)
    # [T-08] _render_kpi_section_v2(_sel_ind)
    # [T-08]
    # [T-08] if _MACRO:
    # [T-08] for label, data in _MACRO.items():
    # [T-08] warn = _validate_macro_item(label, data)
    # [T-08] if warn:
    # [T-08] st.warning(warn)
    # [T-08]
    # [T-08] # 5. 보조 지표 (엔화 / 수출물가 / 수입물가)
    # [T-08] _render_secondary_indicators()
    # [T-08]
    # [T-08] # 6. 전 산업 거시경제 영향 비교표 [Item 2: 종합 임팩트 스코어 추가]
    # [T-08] _render_industry_comparison(_MACRO)
    # [T-08]
    # [T-08] # [Item 1] 산업별 임팩트 랭킹
    # [T-08] if _MACRO:
    # [T-08] _render_industry_impact_ranking(_MACRO)
    # [T-08]
    # [T-08] # 7. 데이터 출처 footer
    # [T-08] st.html("""
    # [T-08] <div style="font-size:10px;color:#94a3b8;text-align:center;margin-top:16px;padding:8px">
    # [T-08] 📊 데이터 출처: 한국은행 ECOS API | 업데이트: 월 1회 | 기사: KDI 나라경제
    # [T-08] </div>
    # [T-08] """)
    # [T-08]
    # [T-08] # ══ TAB 2: 정책브리핑 ════════════════════════════
    # [T-08] with tab2:
    # [T-08] st.session_state.setdefault("docs", [])
    # [T-08] st.session_state.setdefault("selected_id", None)
    # [T-08] st.session_state.setdefault("last_doc", None)
    # [T-08] st.session_state.setdefault("last_detail", None)
    # [T-08] st.session_state.setdefault("docs_fetched_at", "")
    # [T-08]
    # [T-08] # 앱 시작 시 docs가 비어있으면 자동 수집
    # [T-08] _cur_ind = st.session_state.get("selected_industry", "일반")
    # [T-08] if not st.session_state.docs:
    # [T-08] with st.spinner("KDI 나라경제 목록 자동 수집 중..."):
    # [T-08] try:
    # [T-08] _raw = fetch_list(_KDI_URL, 20)
    # [T-08] _rel, _oth = _filter_relevant_docs(_raw, _cur_ind, _cur_sub)
    # [T-08] st.session_state.docs = _rel if _rel else _raw
    # [T-08] st.session_state.docs_others = _oth if _rel else []
    # [T-08] st.session_state.docs_fetched_at = _dt.now().strftime("%Y-%m-%d %H:%M")
    # [T-08] except Exception as _e:
    # [T-08] st.error(f"자동 수집 오류: {_e}")
    # [T-08]
    # [T-08] col_l, col_r = st.columns([2, 3])
    # [T-08]
    # [T-08] # ── 좌: 필터 + 문서 목록 ─────────────────────
    # [T-08] with col_l:
    # [T-08] with st.container(border=True):
    # [T-08] st.markdown("**⚙️ 필터**")
    # [T-08] top_n = st.number_input(
    # [T-08] "목록 수", min_value=5, max_value=50, value=20, step=5,
    # [T-08] key="top_n_input",
    # [T-08] )
    # [T-08] if st.button(
    # [T-08] "🔄 새로 고침", type="primary",
    # [T-08] use_container_width=True, key="btn_load",
    # [T-08] ):
    # [T-08] with st.spinner("목록 수집 중..."):
    # [T-08] try:
    # [T-08] _raw = fetch_list(_KDI_URL, int(top_n))
    # [T-08] _rel, _oth = _filter_relevant_docs(_raw, _cur_ind, _cur_sub)
    # [T-08] st.session_state.docs = _rel if _rel else _raw
    # [T-08] st.session_state.docs_others = _oth if _rel else []
    # [T-08] st.session_state.docs_fetched_at = _dt.now().strftime("%Y-%m-%d %H:%M")
    # [T-08] st.session_state.selected_id = None
    # [T-08] st.session_state.last_doc    = None
    # [T-08] st.session_state.last_detail = None
    # [T-08] if _rel:
    # [T-08] st.toast(f"✅ {len(_rel)}건 관련 기사 필터링 완료 (전체 {len(_raw)}건 중)")
    # [T-08] except Exception as e:
    # [T-08] st.error(f"오류: {e}")
    # [T-08]
    # [T-08] # 요약 캐시 초기화 버튼 (Gemini 재생성용)
    # [T-08] if st.button(
    # [T-08] "🤖 요약 캐시 초기화",
    # [T-08] use_container_width=True, key="btn_clear_cache",
    # [T-08] help="기존 규칙 기반 요약을 지우고 Gemini로 재생성합니다",
    # [T-08] ):
    # [T-08] fetch_detail.clear()
    # [T-08] st.session_state.selected_id = None
    # [T-08] st.session_state.last_doc    = None
    # [T-08] st.session_state.last_detail = None
    # [T-08] st.toast("✅ 요약 캐시 초기화 완료 — 기사 다시 클릭하면 Gemini로 재생성됩니다")
    # [T-08] st.rerun()
    # [T-08]
    # [T-08] docs: list = st.session_state.docs
    # [T-08] if not docs:
    # [T-08] st.info("목록을 불러오는 중입니다...")
    # [T-08] if docs:
    # [T-08] months = sorted({d["issue_yyyymm"] for d in docs}, reverse=True)
    # [T-08] sel_month = st.selectbox("월 필터", ["전체"] + months, key="month_filter")
    # [T-08] kw = st.text_input("키워드 검색", placeholder="제목 내 검색", key="kw_search")
    # [T-08]
    # [T-08] filtered = [
    # [T-08] d for d in docs
    # [T-08] if (sel_month == "전체" or d["issue_yyyymm"] == sel_month)
    # [T-08] and (not kw or kw in d["title"])
    # [T-08] ]
    # [T-08]
    # [T-08] sort_order = st.selectbox("정렬", ["최신순", "오래된순"], key="sort_order")
    # [T-08] fetched_at = st.session_state.get("docs_fetched_at", "")
    # [T-08] if fetched_at:
    # [T-08] st.caption(f"정렬: {sort_order} | 목록 기준: {fetched_at}(KST)")
    # [T-08] else:
    # [T-08] st.caption(f"{len(filtered)}건")
    # [T-08]
    # [T-08] if any(not d.get("issue_yyyymm") for d in filtered):
    # [T-08] st.warning("일부 항목에 날짜 정보 없음")
    # [T-08]
    # [T-08] reverse = (sort_order == "최신순")
    # [T-08] filtered = sorted(filtered, key=lambda d: d.get("issue_yyyymm", ""), reverse=reverse)
    # [T-08]
    # [T-08] st.divider()
    # [T-08]
    # [T-08] # 임팩트 스코어 일괄 산출
    # [T-08] _scored_filtered = score_articles(filtered, _cur_ind, _MACRO)
    # [T-08]
    # [T-08] # 5-C: 빈 목록 방어
    # [T-08] if not _scored_filtered:
    # [T-08] st.info("선택한 필터 조건에 맞는 기사가 없습니다.")
    # [T-08]
    # [T-08] for d in _scored_filtered:
    # [T-08] yyyymm   = d.get("issue_yyyymm", "")
    # [T-08] date_tag = f"[{yyyymm[:4]}.{yyyymm[4:]}] " if len(yyyymm) == 6 else ""
    # [T-08] _impact = d.get("impact_score", 1)
    # [T-08] _stars = "★" * _impact
    # [T-08] _label = f"📄 {date_tag}{_stars} {d['title'][:35]}{'...' if len(d['title']) > 35 else ''}"
    # [T-08]
    # [T-08] # 4~5점 기사: 노란 배경 하이라이트 배지
    # [T-08] if _impact >= 4:
    # [T-08] st.html(
    # [T-08] f'<div style="background:#FFF3CD;border-radius:8px 8px 0 0;'
    # [T-08] f'padding:4px 12px;margin-bottom:-8px;'
    # [T-08] f'font-size:11px;font-weight:700;color:#856404">'
    # [T-08] f'{"★" * _impact} 임팩트 {_impact}점</div>'
    # [T-08] )
    # [T-08]
    # [T-08] btn_type = "primary" if _impact >= 4 else "secondary"
    # [T-08] if st.button(
    # [T-08] _label,
    # [T-08] key=f"doc_{d['doc_id']}",
    # [T-08] use_container_width=True,
    # [T-08] type=btn_type,
    # [T-08] ):
    # [T-08] st.session_state.selected_id = d["doc_id"]
    # [T-08] log_event("article_click", {"doc_id": d["doc_id"], "title": d["title"][:50]})
    # [T-08]
    # [T-08] _render_policy_summary(filtered)
    # [T-08]
    # [T-08] # 관련성 낮은 기사 접기 expander
    # [T-08] _others = st.session_state.get("docs_others", [])
    # [T-08] if _others:
    # [T-08] with st.expander(f"기타 기사 {len(_others)}건 (관련성 낮음)"):
    # [T-08] for _od in _others:
    # [T-08] st.caption(f"📄 {_od['title'][:50]}")
    # [T-08]
    # [T-08] # ── 우: 문서 뷰어 ────────────────────────────
    # [T-08] with col_r:
    # [T-08] docs   = st.session_state.docs
    # [T-08] sel_id = st.session_state.selected_id
    # [T-08]
    # [T-08] if not sel_id:
    # [T-08] st.html(
    # [T-08] '<div style="height:320px;display:flex;align-items:center;'
    # [T-08] 'justify-content:center;border:2px dashed #e2e8f0;'
    # [T-08] 'border-radius:12px;color:#94a3b8;font-size:1rem;background:#f8fafc">'
    # [T-08] "← 왼쪽에서 문서를 선택하세요</div>"
    # [T-08] )
    # [T-08] else:
    # [T-08] doc = next((d for d in docs if d["doc_id"] == sel_id), None)
    # [T-08] if doc:
    # [T-08] with st.spinner("본문 수집 중..."):
    # [T-08] print(f"[app] fetch_detail 요청: doc_id={doc['doc_id']} url={doc['url'][:70]}")
    # [T-08] detail = fetch_detail(doc["doc_id"], doc["url"], doc["title"], industry_key=_cur_ind)
    # [T-08] print(f"[app] fetch_detail 결과: parse_status={detail.get('parse_status')} body_len={detail.get('body_len',0):,} summary_source={detail.get('summary_source','?')}")
    # [T-08]
    # [T-08] # ── LLM 재요약: 캐시에 저장된 "rule" 결과를 Groq 로 업그레이드 ──
    # [T-08] # @st.cache_data 는 함수 반환값 전체를 캐시하므로,
    # [T-08] # 이전에 Groq 키 없이 수집한 기사는 summary_source="rule" 로 캐시돼 있음.
    # [T-08] # 지금 키가 있으면 본문만 꺼내서 LLM 재요약 (HTML 재수집 없이).
    # [T-08] if (
    # [T-08] detail.get("parse_status") == "success"
    # [T-08] and detail.get("summary_source") == "rule"
    # [T-08] and detail.get("body_text")
    # [T-08] ):
    # [T-08] try:
    # [T-08] from core.summarizer import _get_llm_key, summarize_3line as _re_summarize
    # [T-08] if _get_llm_key():
    # [T-08] print("[app] 🔄 캐시된 rule 요약 → Groq 재요약 시도")
    # [T-08] _new_sum, _new_src = _re_summarize(
    # [T-08] detail["body_text"],
    # [T-08] title=doc.get("title", ""),
    # [T-08] industry_key=_cur_ind,
    # [T-08] )
    # [T-08] if _new_src == "groq":
    # [T-08] detail = {
    # [T-08] **detail,
    # [T-08] "summary_3lines":  _new_sum,
    # [T-08] "summary_source":  "groq",
    # [T-08] }
    # [T-08] print(f"[app] ✅ Groq 재요약 완료 ({len(_new_sum)}자)")
    # [T-08] except Exception as _re_err:
    # [T-08] print(f"[app] ⚠️  재요약 오류 (무시): {_re_err}")
    # [T-08]
    # [T-08] st.session_state.last_doc    = doc
    # [T-08] st.session_state.last_detail = detail
    # [T-08]
    # [T-08] with st.container(border=True):
    # [T-08] st.markdown(f"### {doc['title']}")
    # [T-08] _status_label = {
    # [T-08] "success": "✅ 성공",
    # [T-08] "short":   "⚠️ 본문 짧음",
    # [T-08] "fail":    "❌ 수집 실패",
    # [T-08] }.get(detail.get("parse_status", "fail"),
    # [T-08] detail.get("parse_status", ""))
    # [T-08] st.caption(
    # [T-08] f"발행: {doc['issue_yyyymm']} &nbsp;|&nbsp; "
    # [T-08] f"본문: {detail['body_len']:,}자 &nbsp;|&nbsp; "
    # [T-08] f"상태: {_status_label}"
    # [T-08] )
    # [T-08] if doc.get("url"):
    # [T-08] st.markdown(
    # [T-08] f"**🔗 원문**: [{doc['url'][:70]}{'...' if len(doc['url'])>70 else ''}]({doc['url']})"
    # [T-08] )
    # [T-08] # URL 불일치 디버깅용: doc_id와 URL의 cidx 비교
    # [T-08] cidx_in_id  = doc["doc_id"].split("_")[1] if "_" in doc["doc_id"] else ""
    # [T-08] cidx_in_url = re.search(r"cidx=(\d+)", doc["url"])
    # [T-08] if cidx_in_url and cidx_in_id and cidx_in_url.group(1) != cidx_in_id:
    # [T-08] st.warning(
    # [T-08] f"⚠️ doc_id({cidx_in_id})와 URL cidx({cidx_in_url.group(1)})가 다릅니다. "
    # [T-08] f"fetch_detail이 올바른 URL을 사용하는지 확인하세요."
    # [T-08] )
    # [T-08]
    # [T-08] with st.container(border=True):
    # [T-08] st.markdown("**📝 3줄 요약** — 핵심 정책 · 주요 내용 · 영향·시사점")
    # [T-08] _pstatus = detail.get("parse_status", "fail")
    # [T-08] if _pstatus == "success" and detail.get("summary_3lines"):
    # [T-08] _render_summary_3lines(
    # [T-08] detail["summary_3lines"],
    # [T-08] source=detail.get("summary_source", ""),
    # [T-08] )
    # [T-08] else:
    # [T-08] _fail_reason = detail.get("fail_reason", "수집 실패")
    # [T-08] st.warning(f"⚠️ 요약 생성 불가: {_fail_reason}")
    # [T-08] st.markdown(
    # [T-08] "**해결 방법:**  \n"
    # [T-08] "- 📎 아래 **원문 링크**를 직접 클릭해 기사를 확인하세요.  \n"
    # [T-08] "- 🔄 잠시 후 다시 클릭하면 성공할 수 있습니다.  \n"
    # [T-08] "- 🌐 JavaScript 렌더링이 필요한 페이지는 브라우저에서 직접 열어보세요."
    # [T-08] )
    # [T-08]
    # [T-08] if detail.get("keywords"):
    # [T-08] badges = " ".join(
    # [T-08] f'<span style="background:#e8f4fd;color:#1a6fa8;'
    # [T-08] f'padding:2px 8px;border-radius:4px;margin:2px;'
    # [T-08] f'font-size:0.82rem">{k}</span>'
    # [T-08] for k in detail["keywords"]
    # [T-08] )
    # [T-08] st.html(badges)
    # [T-08] st.markdown("")
    # [T-08]
    # [T-08] with st.expander("📄 본문 전체", expanded=False):
    # [T-08] st.text_area(
    # [T-08] "본문 전체", value=detail["body_text"], height=300,
    # [T-08] disabled=True, label_visibility="collapsed",
    # [T-08] key=f"body_{doc['doc_id']}",
    # [T-08] )
    # [T-08]
    # [T-08] _render_policy_industry_impact(doc, _cur_ind)
    # [T-08] _render_article_strategy_questions(doc, _cur_ind)
    # [T-08] _render_policy_detail(doc, detail)
    # [T-08] _render_strategy_questions(doc, detail)
    # [T-08]
    # [T-08] # ══ TAB 3: 리포트 ════════════════════════════════
    # [T-08] with tab3:
    # [T-08] # 선택 문서 JSON 다운로드 (TAB 2에서 이동)
    # [T-08] _sel_doc    = st.session_state.get("last_doc")
    # [T-08] _sel_detail = st.session_state.get("last_detail")
    # [T-08] if _sel_doc and _sel_detail:
    # [T-08] _full_json = json.dumps(
    # [T-08] {**_sel_doc, **_sel_detail}, ensure_ascii=False, indent=2
    # [T-08] )
    # [T-08] st.download_button(
    # [T-08] "⬇️ 선택 문서 JSON 다운로드",
    # [T-08] data=_full_json.encode("utf-8"),
    # [T-08] file_name=f"{_sel_doc['doc_id']}.json",
    # [T-08] mime="application/json",
    # [T-08] key="dl_selected_json",
    # [T-08] use_container_width=True,
    # [T-08] )
    # [T-08]
    # [T-08] _render_content_history()
    # [T-08] _render_download_section(st.session_state.docs)
    # [T-08] === 원본 탭 코드 끝 ===


# ══════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════
render_ui()
