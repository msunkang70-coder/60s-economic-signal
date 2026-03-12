"""
views/main_content.py — 메인 대시보드 콘텐츠 오케스트레이션.
render_ui()에서 추출된 단일 스크롤 레이아웃.
"""
import json
from datetime import date as _date, datetime as _dt

import streamlit as st

from core.ecos import refresh_macro as _ecos_refresh, _get_api_key as _ecos_get_key
from core.analytics import log_event
from core.fetcher import fetch_list, fetch_detail
from core.impact_scorer import score_articles
from core.industry_config import get_profile
from core.action_checklist import generate_checklist
from core.decision_engine import generate_decision_options
from core.today_signal import generate_today_signal
from ui.components import section_header, render_kpi_section_card
from ui.article_cards import (
    render_summary_3lines, render_article_strategy_questions,
    filter_relevant_docs, _INDUSTRY_EXTENDED_KW,
)
from views.company_profile_view import render_fx_impact_widget
from views.dashboard_main import (
    render_dashboard_header, render_impact_strip, render_industry_variable_card,
)
from views.signal_detail import render_today_signal as render_today_signal_card

try:
    from core.plan_gate import gate_feature as _gate_feature
except ImportError:
    def _gate_feature(email, feature): return True  # fallback

_KDI_URL = "https://eiec.kdi.re.kr/publish/naraList.do"


def render_main_content(industry_key: str, macro_data: dict, docs: list) -> None:
    """메인 대시보드 콘텐츠 전체를 렌더링한다.

    Args:
        industry_key: 선택된 산업 키
        macro_data: _MACRO 딕셔너리
        docs: 수집된 기사 목록
    """
    # ── Hero Header ────────────────────────────────────
    render_dashboard_header()

    # ── 충격 감지 배너 ──
    try:
        from core.shock_detector import detect_shocks
        from views.dashboard_main import render_shock_alert_banner
        _shocks = detect_shocks(macro_data)
        if _shocks:
            render_shock_alert_banner(_shocks)
    except Exception:
        pass

    # ── [위치 1] Executive Summary (Hero 바로 아래) ────
    try:
        from views.executive_summary import render_executive_summary
        _exec_signal = generate_today_signal(macro_data, industry_key)
        render_executive_summary(signal=_exec_signal, macro_data=macro_data)
    except Exception:
        pass

    # ── 모닝 브리핑 배너 (Hero Header 아래, 조건부) ────
    if st.session_state.get("brief_enabled", False):
        try:
            from core.morning_brief import generate_morning_brief
            from views.morning_brief_ui import render_morning_brief_banner
            if macro_data:
                _company_profile = st.session_state.get("company_profile")
                _brief = generate_morning_brief(macro_data, industry_key, _company_profile)
                render_morning_brief_banner(_brief)
        except Exception:
            pass

    # ── 워치리스트 발동 배너 (세션당 1회 확인) ───────────
    try:
        from core.watchlist import check_watchlist
        _triggered = check_watchlist(macro_data) if macro_data else []
        if _triggered:
            _banner_items = " · ".join(
                f"{it['indicator']} {it.get('current_value', '')} "
                f"({'>' if it.get('condition') == 'above' else '<'} {it['threshold']})"
                for it in _triggered[:3]
            )
            st.warning(
                f"⚠️ **워치리스트 알림 발동** — {_banner_items}  "
                f"| 하단 [6. 워치리스트 설정] 에서 확인하세요.",
                icon="🔔",
            )
    except Exception:
        pass

    # ── ECOS 업데이트 + PDF 다운로드 버튼 ───────────────
    _has_key = bool(_ecos_get_key())
    _, col_pdf, col_btn = st.columns([6, 1, 1])
    with col_pdf:
        try:
            from views.pdf_report_view import render_pdf_download_button
            _sig = generate_today_signal(macro_data, industry_key) if macro_data else None
            _opts = generate_decision_options(macro_data, industry_key, _sig) if macro_data else []
            render_pdf_download_button(macro_data, industry_key, _sig, _opts, [])
        except Exception:
            pass
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
                    try:
                        from core.impact_scorer import (
                            calculate_macro_impact_score,
                            update_and_get_score_delta,
                        )
                        from core.industry_config import INDUSTRY_PROFILES
                        from core.macro_utils import _load_macro
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

    # ── [1] 오늘의 핵심 신호 ────────────────────────────
    section_header("⚡ 오늘의 핵심 신호", "현재 경제 상황 요약 및 즉시 행동 가이드", "1")
    render_today_signal_card(industry_key, macro_data)

    # ── [T-22] Impact Strip (신호등 스트립) ──
    render_impact_strip(macro_data)

    # ── [2] 핵심 지표 KPI 카드 4종 ──────────────────────
    section_header("📊 핵심 지표 KPI", "ECOS 한국은행 공식 거시경제 지표", "2")
    if macro_data:
        _kpi_keys = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
        _kpi_items = [(k, macro_data[k]) for k in _kpi_keys if k in macro_data]
        if _kpi_items:
            render_kpi_section_card(_kpi_items)
        # Macro validation warnings
        from core.macro_utils import _validate_macro_item
        for _m_label, _m_data in macro_data.items():
            _m_warn = _validate_macro_item(_m_label, _m_data)
            if _m_warn:
                st.warning(_m_warn)
    else:
        st.info("거시지표 데이터 없음 — ECOS API 키 설정 후 업데이트 버튼을 클릭하세요.")

    # ── [2.1] 기업 맞춤 영향 위젯 (조건부) ─────────────────
    _cp_v2 = st.session_state.get("company_profile_v2") or st.session_state.get("company_profile")
    if _cp_v2 and float(_cp_v2.get("annual_revenue_100m", 0)) > 0 and macro_data:
        render_fx_impact_widget(_cp_v2, macro_data)

    # ── 복합 리스크 지수 ──
    try:
        from core.risk_index import calculate_risk_index
        from views.dashboard_main import render_risk_gauge
        _risk = calculate_risk_index(macro_data, industry_key)
        render_risk_gauge(_risk)
    except Exception:
        pass

    # ── [위치 2] 벤치마크 카드 (KPI 아래, divider 위) ─────
    _cp_bench = st.session_state.get("company_profile_v2") or st.session_state.get("company_profile")
    if _cp_bench and macro_data:
        try:
            from views.benchmark_widget import render_benchmark_card
            render_benchmark_card(
                company_profile=_cp_bench,
                industry_key=industry_key,
                macro_data=macro_data,
            )
        except Exception:
            pass

    st.divider()

    # ── [2.5] 시나리오 분석 ──────────────────────────────
    with st.expander("🔮 시나리오 분석", expanded=False):
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

            if _sc_run and macro_data:
                _sc_result = simulate_scenario(macro_data, _sc_selected, industry_key)

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
            elif _sc_run and not macro_data:
                st.warning("거시지표 데이터가 없습니다. ECOS 업데이트를 먼저 실행하세요.")

            # ── 시나리오 전략 옵션 (decision_engine 연동) ──
            try:
                from core.decision_engine import generate_scenario_strategies, _SCENARIO_PRESETS

                _scenario_names = list(_SCENARIO_PRESETS.keys())
                _selected_scenario = st.selectbox("시나리오 전략 선택", _scenario_names, key="scenario_select")

                _strategies = generate_scenario_strategies(macro_data, industry_key, _selected_scenario)
                if _strategies:
                    for i, opt in enumerate(_strategies[:3], 1):
                        urgency_color = "#dc2626" if opt.get("urgency") == "즉시" else "#ea580c" if opt.get("urgency") == "이번 주" else "#16a34a"
                        st.markdown(f"""
                        <div style="padding:12px; margin:8px 0; background:#fff; border-radius:8px; border-left:4px solid {urgency_color};">
                            <strong>{i}. {opt.get('title','')}</strong><br>
                            <span style="color:#666; font-size:13px;">{opt.get('rationale','')}</span><br>
                            <span style="background:{urgency_color}; color:#fff; padding:2px 8px; border-radius:4px; font-size:11px;">{opt.get('urgency','')}</span>
                            <span style="background:#e8f5e9; color:#375623; padding:2px 8px; border-radius:4px; font-size:11px; margin-left:4px;">난이도: {opt.get('difficulty','')}</span>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.caption("해당 시나리오에 대한 전략을 생성할 수 없습니다.")
            except Exception:
                st.caption("시나리오 분석 모듈 로딩 중...")

        except Exception as _sc_err:
            st.error(f"시나리오 분석 오류: {_sc_err}")

    # ── [2.6] 글로벌 시장 추천 ───────────────────────────
    with st.expander("🌏 글로벌 시장 추천", expanded=False):
        st.markdown("""
<div style="padding:20px; text-align:center; background:#f8fafc; border-radius:8px; border:1px dashed #cbd5e1;">
    <p style="font-size:16px; color:#64748b; margin:0;">🌍 Coming Soon</p>
    <p style="font-size:13px; color:#94a3b8; margin:8px 0 0;">관세청 데이터 연동 후 활성화됩니다</p>
</div>
        """, unsafe_allow_html=True)

    st.divider()

    # ── [3] 산업별 핵심 변수 카드 ────────────────────────
    section_header("🔬 산업별 핵심 변수", "선택 산업의 경제 민감 변수 실시간 모니터링", "3")
    render_industry_variable_card(industry_key, docs, macro_data)

    st.divider()

    # ── [3.5] 전략 옵션 (Decision Engine) ────────────────
    _today_sig = generate_today_signal(macro_data, industry_key)
    _decision_opts = generate_decision_options(macro_data, industry_key, _today_sig)
    if _decision_opts:
        # 현재 신호 컨텍스트 서브타이틀 생성
        _sig_label = _today_sig.get("label", "") if _today_sig else ""
        _sig_val   = _today_sig.get("value", "") if _today_sig else ""
        _sig_trend = _today_sig.get("trend", "") if _today_sig else ""
        _status_badge = {"warning": "⚠ 경고", "danger": "🔴 위험",
                         "caution": "🟡 주의", "normal": "🟢 정상"}
        from core.macro_utils import _get_threshold_status
        _sig_status, _, _ = _get_threshold_status(_sig_label, str(_sig_val)) if _sig_label else ("normal", "", "")
        _sig_badge  = _status_badge.get(_sig_status, "")
        _sig_ctx = f"{_sig_label} {_sig_val} {_sig_trend} {_sig_badge}" if _sig_label else "현재 경제 신호 기반"
        section_header("🎯 전략 옵션", f"근거 신호: {_sig_ctx} — 기업이 취할 수 있는 전략 3가지", "4")
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

    # ── [4] 주요 기사 목록 (임팩트 스코어 내림차순) ──────
    _render_article_list(industry_key, macro_data)


def _render_article_list(industry_key: str, macro_data: dict) -> None:
    """주요 기사 목록 렌더링 — render_ui() 1398~1651줄 추출."""
    section_header("📰 주요 기사 목록", "KDI 나라경제 + 뉴스 RSS — 임팩트 스코어 내림차순", "5")

    # 기사 자동 수집 (session state 초기화)
    st.session_state.setdefault("docs", [])
    st.session_state.setdefault("selected_id", None)
    st.session_state.setdefault("last_doc", None)
    st.session_state.setdefault("last_detail", None)
    st.session_state.setdefault("docs_fetched_at", "")

    _cur_ind = industry_key
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
                _rel, _oth = filter_relevant_docs(_raw, _cur_ind)
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
                    try:
                        from core.extra_sources import fetch_all_sources
                        _raw, _src_stats = fetch_all_sources(_raw, kotra_max=5, industry_key=_cur_ind)
                        print(f"[extra_sources] source_stats: {_src_stats}")
                    except Exception as _extra_e:
                        print(f"[extra_sources] 통합 실패, KDI만 사용: {_extra_e}")
                    _rel, _oth = filter_relevant_docs(_raw, _cur_ind)
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

    # ── [위치 3] 기사 프리페치 (docs 로드 후 1회) ─────────
    if docs and not st.session_state.get("prefetch_started"):
        try:
            from core.prefetch_worker import prefetch_top_articles
            prefetch_top_articles(docs, n=5, industry_key=_cur_ind)
            st.session_state["prefetch_started"] = True
        except Exception:
            pass

    if docs:
        # 임팩트 스코어 일괄 산출 + 내림차순 정렬
        _scored_docs = score_articles(docs, _cur_ind, macro_data)
        _scored_docs = sorted(_scored_docs, key=lambda d: d.get("impact_score", 1), reverse=True)

        # 키워드 필터 적용
        if _scroll_kw:
            _scored_docs = [d for d in _scored_docs if _scroll_kw in d.get("title", "")]

        # ── 출처 필터 ────────────────────────────────────
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
            # 산업 관련 기사 수 표시
            _ind_profile = get_profile(_cur_ind)
            _ind_kws = _ind_profile.get("keywords", [])
            _ext_kws = _INDUSTRY_EXTENDED_KW.get(_cur_ind, [])
            _all_ind_kws = _ind_kws + _ext_kws
            if _all_ind_kws and _cur_ind != "일반":
                _ind_match_count = sum(
                    1 for d in _scored_docs
                    if any(kw in d.get("title", "") for kw in _all_ind_kws)
                )
                st.caption(
                    f"기사 {len(_scored_docs)}건 "
                    f"({_ind_profile['icon']} 산업 관련 {_ind_match_count}건) "
                    f"| 기준: {_fetched_at}(KST) | 임팩트 스코어 높은 순"
                )
            else:
                st.caption(f"기사 {len(_scored_docs)}건 | 기준: {_fetched_at}(KST) | 임팩트 스코어 높은 순")

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

        # 출처 배지 맵
        _SRC_BADGE_MAP = {
            "KDI": ("background:#dbeafe;color:#1e40af;border:1px solid #93c5fd", "KDI"),
            "연합뉴스경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "연합뉴스"),
            "매일경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "매일경제"),
            "한국경제": ("background:#dcfce7;color:#166534;border:1px solid #86efac", "한국경제"),
            "산업부": ("background:#fff7ed;color:#9a3412;border:1px solid #fdba74", "산업부"),
        }

        # T-09: 이메일 앵커 링크 처리
        _target_article_id = st.query_params.get("article_id", None)

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
            _auto_expand = _is_target or (_art_idx <= _TOP_N and not _show_all)
            if _is_target:
                try:
                    log_event("article_click", {"doc_id": _art.get("doc_id", ""), "title": _art_title[:50], "source": "email"})
                except Exception:
                    pass

            # 출처 태그
            _art_source = _art.get("source", "KDI")
            _src_tag = f" [{_art_source}]" if _art_source and _art_source != "KDI" else ""
            with st.expander(f"[{_art_stars}] {_art_date}{_art_title}{_src_tag}", expanded=_auto_expand):
                _badge_style, _badge_text = _SRC_BADGE_MAP.get(
                    _art_source, ("background:#f1f5f9;color:#475569;border:1px solid #e2e8f0", _art_source)
                )
                st.html(
                    f'<div style="height:3px;background:{_accent};border-radius:2px;margin-bottom:8px"></div>'
                    f'<span style="{_badge_style};font-size:10px;font-weight:700;'
                    f'padding:2px 8px;border-radius:8px">{_badge_text}</span>'
                )

                # 기사 상세 로드 — [위치 4] 캐시 우선 조회
                with st.spinner("본문 수집 중..."):
                    try:
                        # ArticleCache 캐시 우선 조회
                        _art_detail = None
                        _cache_key = f"{_art['doc_id']}_{_cur_ind}"
                        try:
                            from core.article_cache import get_cache
                            _cache = get_cache()
                            _cached = _cache.get(_cache_key)
                            if _cached:
                                _art_detail = _cached
                        except Exception:
                            pass

                        if _art_detail is None:
                            _art_detail = fetch_detail(
                                _art["doc_id"], _art["url"], _art["title"],
                                industry_key=_cur_ind,
                            )
                            # 캐시에 저장 (산업별 키)
                            try:
                                _cache.set(_cache_key, _art_detail)
                            except Exception:
                                pass
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
                    # headline 표시 (4-frame dict인 경우)
                    _sum_data = _art_detail.get("summary_3lines")
                    _headline = _sum_data.get("headline", "") if isinstance(_sum_data, dict) else ""
                    if _headline:
                        st.markdown(f"**\U0001f4cc {_headline}**")

                    # 4-frame 요약
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
                                # Action 필드: bullet point가 있으면 리스트 렌더링
                                if _fl == "✅ Action" and "•" in _ft:
                                    _bullets = [b.strip() for b in _ft.split("•") if b.strip()]
                                    _bullet_html = "".join(
                                        f'<div style="font-size:13px;color:#334155;margin-top:3px;padding-left:8px">'
                                        f'• {b}</div>'
                                        for b in _bullets
                                    )
                                    _frame_html += (
                                        f'<div style="padding:8px 12px;border-left:3px solid {_fc};'
                                        f'margin-bottom:6px;background:rgba(0,0,0,0.02);border-radius:0 8px 8px 0">'
                                        f'<span style="font-size:11px;font-weight:700;color:{_fc}">{_fl}</span>'
                                        f'{_bullet_html}</div>'
                                    )
                                else:
                                    # 기존 렌더링: bold 마크다운(**text**) → <strong> 변환
                                    import re as _re_render
                                    _ft_html = _re_render.sub(
                                        r'\*\*(.+?)\*\*',
                                        r'<strong style="color:#1E293B">\1</strong>',
                                        _ft
                                    )
                                    _frame_html += (
                                        f'<div style="padding:8px 12px;border-left:3px solid {_fc};'
                                        f'margin-bottom:6px;background:rgba(0,0,0,0.02);border-radius:0 8px 8px 0">'
                                        f'<span style="font-size:11px;font-weight:700;color:{_fc}">{_fl}</span>'
                                        f'<div style="font-size:13px;color:#334155;margin-top:2px;line-height:1.6">{_ft_html}</div></div>'
                                    )
                        if _frame_html:
                            st.html(f'<div style="font-family:Inter,sans-serif">{_frame_html}</div>')
                    else:
                        _pstatus = _art_detail.get("parse_status", "fail")
                        if _pstatus == "success" and _sum_data:
                            render_summary_3lines(
                                _sum_data,
                                source=_art_detail.get("summary_source", ""),
                            )
                        else:
                            st.warning(f"⚠️ 요약 생성 불가: {_art_detail.get('fail_reason', '수집 실패')}")

                    # 전략 질문
                    render_article_strategy_questions(_art, _cur_ind)

                    # 체크리스트
                    try:
                        _art_checklist = generate_checklist(
                            _art.get("title", ""), _cur_ind, macro_data
                        )
                        if _art_checklist:
                            st.markdown("**✅ 액션 체크리스트**")
                            for _cl_item in _art_checklist:
                                st.checkbox(_cl_item, key=f"cl_{_art['doc_id']}_{_cl_item[:20]}")
                    except Exception:
                        pass

                    # 원문 링크
                    _source_url = (
                        _art_detail.get("url")
                        or _art_detail.get("source_url")
                        or _art.get("url")
                        or _art.get("link", "")
                    )
                    if not _source_url or _source_url.strip() == "":
                        _source_url = None
                    if _source_url and _source_url.startswith("http"):
                        st.markdown(f"\U0001f517 [원문 보기]({_source_url})")
                    else:
                        st.caption("📎 원문 링크 없음")

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
            with st.expander(f"기타 기사 {len(_others)}건 (관련성 낮음)"):
                for _od in _others:
                    st.caption(f"📄 {_od['title'][:50]}")
    elif not st.session_state.docs:
        st.info("목록을 불러오는 중입니다...")


def render_email_send_section(industry_key: str) -> None:
    """다운로드 섹션 아래 이메일 발송 버튼 렌더링."""
    try:
        from core.emailer import is_configured as _email_ok2, send_report_email as _send_report2
        _email_configured2 = _email_ok2()
    except Exception:
        _email_configured2 = False

    if not _email_configured2:
        return

    from core.report import generate_report_html as _gen_report_html
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
                _html2 = _gen_report_html(_docs_for_email2, _doc_for_email2, _detail_for_email2)
                _profile_label2 = get_profile(industry_key).get("label", industry_key)
                _subject2 = (
                    f"[{_date.today().strftime('%Y-%m-%d')}] "
                    f"{_profile_label2} 경제신호 리포트"
                )
                _ok2 = _sre(_html2, _subject2)
                if _ok2:
                    st.toast("✅ 이메일 발송 완료!")
                    log_event("report_email_sent", {"industry": industry_key})
                else:
                    st.error("발송 실패 — 이메일 설정을 확인하세요")
            except Exception as _e2:
                st.error(f"발송 오류: {_e2}")


def render_watchlist_section(industry_key: str) -> None:
    """워치리스트 설정 섹션을 렌더링한다."""
    st.divider()
    section_header("⚙️ 워치리스트 설정", "임계값 초과 시 이메일 알림", "6")
    _wl_cap_col1, _wl_cap_col2 = st.columns([4, 1])
    with _wl_cap_col1:
        st.caption("거시지표가 설정한 임계값을 초과하면 이메일 알림을 받습니다.")
    with _wl_cap_col2:
        st.page_link("pages/7_알림_설정.py", label="🔔 채널 설정", icon="⚙️")

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
                        industry_keys=[industry_key] if industry_key != "일반" else [],
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
            # 워치리스트 테이블 — custom HTML
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


def render_executive_briefing_card(macro_data: dict, industry_key: str) -> None:
    """경영진 브리핑 카드.

    주요 거시지표 현황과 산업별 시사점을 경영진 관점으로 요약 표시.
    """
    if not macro_data:
        return

    profile = get_profile(industry_key)
    industry_label = profile.get("label", industry_key)
    industry_icon = profile.get("icon", "📊")

    # 핵심 지표 요약 수집
    _brief_keys = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
    summary_items = []
    for k in _brief_keys:
        d = macro_data.get(k)
        if d and isinstance(d, dict):
            val = d.get("value", "—")
            trend = d.get("trend", "")
            summary_items.append(f"{k}: {val} {trend}")

    if not summary_items:
        return

    items_html = "".join(
        f'<div style="padding:6px 0;font-size:13px;color:#334155;'
        f'border-bottom:1px solid #f1f5f9">{item}</div>'
        for item in summary_items
    )

    # AI 시사점 (try/except for optional LLM)
    insight_html = ""
    try:
        signal = generate_today_signal(macro_data, industry_key)
        if signal and signal.get("interpretation"):
            insight_html = (
                f'<div style="margin-top:12px;padding:10px 14px;background:#F4F4FF;'
                f'border-radius:8px;border-left:3px solid #5B5FEE;'
                f'font-size:12px;color:#3D40C4;line-height:1.6">'
                f'💡 {signal["interpretation"]}</div>'
            )
    except Exception:
        pass

    st.html(f"""
    <div style="background:white;border:1px solid #e2e8f0;border-radius:12px;
                padding:20px 24px;margin-bottom:16px;font-family:'Inter',sans-serif;
                box-shadow:0 2px 8px rgba(0,0,0,0.04)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div style="font-size:15px;font-weight:800;color:#1E1B4B">
          {industry_icon} {industry_label} 경영진 브리핑
        </div>
        <span style="background:#5B5FEE;color:white;font-size:10px;font-weight:700;
                     padding:3px 10px;border-radius:12px">EXECUTIVE</span>
      </div>
      {items_html}
      {insight_html}
    </div>
    """)
