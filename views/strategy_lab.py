"""views/strategy_lab.py — Strategy Lab: 시나리오 기반 전략 비교·탐색 뷰."""
import streamlit as st

from core.decision_engine import (
    _SCENARIO_PRESETS,
    compare_strategies,
    generate_decision_options,
    generate_scenario_strategies,
)
from core.industry_config import get_profile
from core.today_signal import generate_today_signal
from ui.components import section_header


# ── 시나리오 선택기 ──────────────────────────────────────
def render_scenario_selector() -> str:
    """사이드바 또는 메인 영역에서 시나리오를 선택. 선택된 시나리오 이름 반환."""
    scenarios = list(_SCENARIO_PRESETS.keys())
    selected = st.selectbox(
        "시나리오 선택",
        scenarios,
        index=0,
        key="strategy_lab_scenario",
    )
    return selected


# ── 전략 비교 렌더링 ─────────────────────────────────────
def render_strategy_comparison(
    strategies_a: list[dict], strategies_b: list[dict]
) -> None:
    """두 전략 세트(A=현재, B=시나리오)를 비교 카드로 렌더링."""
    comparison = compare_strategies(strategies_a, strategies_b)

    section_header("전략 비교 분석", subtitle="현재 vs 시나리오 전략 비교")

    # 공통 테마
    common = comparison.get("common_themes", [])
    divergences = comparison.get("divergences", [])
    urgency_shift = comparison.get("urgency_shift", 0)
    risk_delta = comparison.get("risk_delta", 0)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**공통 전략 테마**")
        if common:
            for theme in common:
                st.markdown(f"- {theme}")
        else:
            st.caption("공통 전략 없음")

    with col2:
        st.markdown("**차별화 전략**")
        if divergences:
            for d in divergences:
                st.markdown(f"- {d}")
        else:
            st.caption("차이 없음")

    # 긴급도·영향도 변화
    urg_label = "상승" if urgency_shift > 0 else "하락" if urgency_shift < 0 else "동일"
    risk_label = "상승" if risk_delta > 0 else "하락" if risk_delta < 0 else "동일"

    m1, m2 = st.columns(2)
    m1.metric("긴급도 변화", urg_label, delta=urgency_shift)
    m2.metric("영향도 변화", risk_label, delta=risk_delta)


# ── 전략 타임라인 ────────────────────────────────────────
def render_strategy_timeline(industry_key: str) -> None:
    """산업별 전 시나리오 전략을 긴급도 순서(즉시→이번 주→이번 달)로 타임라인 표시."""
    section_header("전략 타임라인", subtitle=f"{industry_key} — 시나리오별 실행 순서")

    urgency_order = {"즉시": 0, "이번 주": 1, "이번 달": 2}
    all_strategies: list[dict] = []

    for scenario_name in _SCENARIO_PRESETS:
        strategies = generate_scenario_strategies({}, industry_key, scenario_name)
        all_strategies.extend(strategies)

    if not all_strategies:
        st.info("표시할 전략이 없습니다.")
        return

    all_strategies.sort(key=lambda s: urgency_order.get(s.get("urgency", "이번 달"), 2))

    for urg_label in ["즉시", "이번 주", "이번 달"]:
        items = [s for s in all_strategies if s.get("urgency") == urg_label]
        if not items:
            continue
        st.markdown(f"#### {urg_label}")
        for item in items:
            scenario_tag = item.get("scenario", "")
            st.markdown(
                f"- **{item['title']}** ({scenario_tag}) — {item.get('rationale', '')}"
            )


# ── 메인 Strategy Lab 렌더링 ─────────────────────────────
def render_strategy_lab(macro_data: dict, industry_key: str) -> None:
    """Strategy Lab 전체 뷰: 시나리오 선택 → 전략 생성 → 비교 → 타임라인."""
    profile = get_profile(industry_key)
    section_header(
        "Strategy Lab",
        subtitle=f"{profile['icon']} {profile['label']} — 시나리오 기반 전략 탐색",
        number="S",
    )

    # 현재 전략 (baseline)
    cp = st.session_state.get("company_profile") or {}
    signal = generate_today_signal(macro_data, industry_key, company_profile=cp) if macro_data else None
    baseline = generate_decision_options(
        macro_data, industry_key, signal, company_profile=cp
    ) if signal else []

    # 시나리오 선택
    scenario = render_scenario_selector()
    preset = _SCENARIO_PRESETS.get(scenario, {})

    st.caption(
        f"시나리오: **{scenario}** — {preset.get('label', '')} = {preset.get('value', '')}"
    )

    # 시나리오 전략 생성
    scenario_strategies = generate_scenario_strategies(macro_data, industry_key, scenario)

    # 양쪽 전략 카드 표시
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("##### 현재 전략")
        if baseline:
            for opt in baseline:
                st.markdown(
                    f"**{opt['option']}. {opt['title']}**  \n"
                    f"{opt.get('rationale', '')}  \n"
                    f"긴급도: {opt.get('urgency', '-')} | 난이도: {opt.get('difficulty', '-')} | 영향: {opt.get('impact', '-')}"
                )
        else:
            st.info("현재 데이터 기반 전략 없음")

    with col_b:
        st.markdown(f"##### 시나리오: {scenario}")
        if scenario_strategies:
            for opt in scenario_strategies:
                st.markdown(
                    f"**{opt['option']}. {opt['title']}**  \n"
                    f"{opt.get('rationale', '')}  \n"
                    f"긴급도: {opt.get('urgency', '-')} | 난이도: {opt.get('difficulty', '-')} | 영향: {opt.get('impact', '-')}"
                )
        else:
            st.info("시나리오 전략 없음")

    st.divider()

    # 비교
    if baseline or scenario_strategies:
        render_strategy_comparison(baseline, scenario_strategies)

    st.divider()

    # 타임라인
    render_strategy_timeline(industry_key)
