"""
views/company_profile_view.py — 기업 프로파일 입력 폼 및 영향 위젯 UI
(Agent 3: Company Custom Metrics)
"""

import streamlit as st

from core.company_profile_v2 import (
    load_company_profile,
    save_company_profile,
    calculate_fx_impact,
    calculate_raw_material_impact,
    _DEFAULT_PROFILE,
    _RAW_MATERIAL_SENSITIVITY,
)

_RAW_MATERIAL_OPTIONS = list(_RAW_MATERIAL_SENSITIVITY.keys())

_SEGMENT_MAP = {
    "반도체": ["전체", "메모리", "파운드리", "팹리스", "소재·장비"],
    "자동차": ["전체", "완성차", "부품·소재", "전기차", "자율주행"],
    "화학":   ["전체", "석유화학", "정밀화학", "바이오화학", "소재"],
    "소비재": ["전체", "식품·음료", "패션·뷰티", "가전·IT", "생활용품"],
    "일반":   ["전체"],
}

_SIZE_OPTIONS = ["설정 안 함", "스타트업/소기업", "중소기업", "중견·대기업"]
_MARKET_OPTIONS = ["미국", "중국", "EU", "동남아", "일본", "중동", "기타"]


def render_company_profile_form(industry_key: str) -> dict | None:
    """사이드바에 삽입되는 기업 정보 입력 폼.

    Args:
        industry_key: 현재 선택된 산업 키

    Returns:
        설정된 프로파일 dict 또는 None
    """
    with st.container(border=True):
        st.markdown("#### 🏢 기업 컨텍스트")
        st.caption("설정하면 산업·규모에 맞는 맞춤 전략을 받을 수 있습니다")

        # 저장된 프로파일 로드
        _saved = load_company_profile()

        # ── 기본 3필드 (항상 표시) ────────────────────────
        # 기업명
        _company_name = st.text_input(
            "기업명",
            value=_saved.get("company_name", ""),
            placeholder="(주)예시기업",
            key="cp_company_name",
        )

        # 연간 매출 규모 (억 원)
        _annual_rev = st.number_input(
            "연간 매출 (억 원)",
            min_value=0,
            max_value=1_000_000,
            value=int(_saved.get("annual_revenue_100m", 0)),
            step=100,
            key="cp_annual_revenue",
            help="억 원 단위로 입력 (예: 1000 = 1,000억 원)",
        )

        # 수출 비중 (%)
        _export_pct = st.slider(
            "수출 비중 (%)",
            min_value=0,
            max_value=100,
            value=int(_saved.get("export_ratio_pct", 0)),
            step=5,
            key="cp_export_ratio",
        )

        # ── 상세 설정 (expander 안) ─────────────────────
        with st.expander("상세 설정", expanded=False):
            # 세그먼트 — 산업 선택에 따라 동적 변경
            _seg_options = _SEGMENT_MAP.get(industry_key, ["전체"])
            _segment = st.selectbox(
                "세그먼트",
                _seg_options,
                key="segment",
            ) if len(_seg_options) > 1 else "전체"

            # 달러 결제 비중 (%)
            _dollar_pct = st.slider(
                "달러 결제 비중 (%)",
                min_value=0,
                max_value=100,
                value=int(_saved.get("dollar_payment_ratio_pct", 0)),
                step=5,
                key="cp_dollar_payment",
            )

            # 주요 원자재
            _raw_materials = st.multiselect(
                "주요 원자재",
                _RAW_MATERIAL_OPTIONS,
                default=_saved.get("raw_materials", []),
                key="cp_raw_materials",
                placeholder="원자재 선택 (복수 가능)",
            )

            # 주요 수출 시장
            _key_markets = st.multiselect(
                "주요 수출 시장",
                _MARKET_OPTIONS,
                default=_saved.get("key_markets", []),
                key="cp_key_markets",
                placeholder="시장 선택 (복수 가능)",
            )

            # 기업 규모
            _saved_size = _saved.get("company_size", "중견·대기업")
            _size_idx = _SIZE_OPTIONS.index(_saved_size) if _saved_size in _SIZE_OPTIONS else 0
            _company_size = st.selectbox(
                "기업 규모",
                _SIZE_OPTIONS,
                index=_size_idx,
                key="cp_company_size",
            )

        # 저장 버튼
        if st.button("💾 프로파일 저장", use_container_width=True, key="btn_save_profile"):
            _profile = {
                "company_name": _company_name,
                "industry_key": industry_key,
                "annual_revenue_100m": _annual_rev,
                "export_ratio_pct": _export_pct,
                "dollar_payment_ratio_pct": _dollar_pct,
                "raw_materials": _raw_materials,
                "key_markets": _key_markets,
                "company_size": _company_size,
                "segment": _segment,
            }
            save_company_profile(_profile)
            st.session_state["company_profile_v2"] = _profile
            st.toast("✅ 기업 프로파일이 저장되었습니다!")

        # 프로파일 적용 여부 판단
        _profile_set = (
            _annual_rev > 0
            or _export_pct > 0
            or _dollar_pct > 0
            or _company_size != "설정 안 함"
            or bool(_key_markets)
        )

        if _profile_set:
            _company_profile = {
                "company_name": _company_name,
                "industry_key": industry_key,
                "annual_revenue_100m": _annual_rev,
                "export_ratio_pct": _export_pct,
                "dollar_payment_ratio_pct": _dollar_pct,
                "raw_materials": _raw_materials,
                "key_markets": _key_markets,
                "company_size": _company_size,
                "segment": _segment,
                # 기존 호환: 레거시 필드
                "export_ratio": (
                    "70% 이상" if _export_pct >= 70
                    else "30–70%" if _export_pct >= 30
                    else "30% 미만" if _export_pct > 0
                    else "설정 안 함"
                ),
                "export_currency": ["USD"],
                "main_market": _key_markets,
            }
            st.session_state["company_profile"] = _company_profile
            st.session_state["company_profile_v2"] = _company_profile
            st.success("✅ 프로파일 적용됨")
            return _company_profile

        st.session_state.pop("company_profile", None)
        st.session_state.pop("company_profile_v2", None)
        return None


def render_fx_impact_widget(profile: dict, macro_data: dict) -> None:
    """KPI 카드 아래에 삽입되는 '귀사 영향' 계산 위젯.

    Args:
        profile: 기업 프로파일 dict
        macro_data: 거시지표 dict
    """
    if not profile or not macro_data:
        return

    # 프로파일 미입력 가드 — 핵심 필드가 모두 기본값이면 안내만 표시
    _guard_profile = st.session_state.get("company_profile") or profile
    if _guard_profile and all(
        _guard_profile.get(k, 0) in (0, 0.0, "", "설정 안 함", None)
        for k in ["export_ratio_pct", "dollar_payment_ratio_pct", "annual_revenue_100m"]
    ):
        st.info("💡 기업 프로파일을 설정하면 업종 대비 맞춤 벤치마킹을 볼 수 있습니다.")
        return

    annual_rev = float(profile.get("annual_revenue_100m", 0))
    if annual_rev <= 0:
        return

    company = profile.get("company_name", "귀사") or "귀사"

    # ── 환율 영향 계산 ──
    fx_data = macro_data.get("환율(원/$)")
    fx_html_parts = []
    if fx_data:
        try:
            current_fx = float(str(fx_data.get("value", "0")).replace(",", ""))
            prev_fx = float(str(fx_data.get("prev_value", "0")).replace(",", ""))
            fx_change_pct = ((current_fx - prev_fx) / prev_fx * 100) if prev_fx else 0

            if abs(fx_change_pct) > 0.01:
                fx_result = calculate_fx_impact(profile, fx_change_pct)
                monthly_net = fx_result["monthly_net_100m"]
                net_color = "#16a34a" if monthly_net >= 0 else "#dc2626"
                net_sign = "+" if monthly_net >= 0 else ""
                fx_html_parts.append(f"""
                <div style="display:flex;justify-content:space-between;align-items:center;
                            padding:8px 0;border-bottom:1px solid #f1f5f9">
                    <div>
                        <div style="font-size:11px;color:#64748b">💱 환율 변동 영향</div>
                        <div style="font-size:12px;color:#475569;margin-top:2px">
                            {current_fx:,.0f}원/$ (전기 대비 {fx_change_pct:+.2f}%)
                        </div>
                    </div>
                    <div style="text-align:right">
                        <div style="font-size:18px;font-weight:800;color:{net_color}">
                            {net_sign}{monthly_net:.1f}억
                        </div>
                        <div style="font-size:10px;color:#94a3b8">월간 순영향</div>
                    </div>
                </div>
                """)
        except (ValueError, TypeError):
            pass

    # ── 물가(CPI) 영향 계산 ──
    cpi_data = macro_data.get("소비자물가(CPI)")
    cpi_html_parts = []
    if cpi_data:
        try:
            cpi_val = float(str(cpi_data.get("value", "0")).replace(",", "").replace("+", ""))
            if cpi_val > 0:
                cpi_result = calculate_raw_material_impact(profile, cpi_val)
                monthly_cost = cpi_result["monthly_cost_impact_100m"]
                cost_color = "#dc2626" if monthly_cost > 0 else "#16a34a"
                cost_sign = "+" if monthly_cost >= 0 else ""
                raw_mats = profile.get("raw_materials", [])
                mat_label = ", ".join(raw_mats[:2]) if raw_mats else "일반원가"
                cpi_html_parts.append(f"""
                <div style="display:flex;justify-content:space-between;align-items:center;
                            padding:8px 0">
                    <div>
                        <div style="font-size:11px;color:#64748b">📦 원가 영향 ({mat_label})</div>
                        <div style="font-size:12px;color:#475569;margin-top:2px">
                            CPI {cpi_val:.1f}% 반영
                        </div>
                    </div>
                    <div style="text-align:right">
                        <div style="font-size:18px;font-weight:800;color:{cost_color}">
                            {cost_sign}{monthly_cost:.1f}억
                        </div>
                        <div style="font-size:10px;color:#94a3b8">월간 원가 영향</div>
                    </div>
                </div>
                """)
        except (ValueError, TypeError):
            pass

    # 표시할 내용이 있을 때만 렌더링
    if not fx_html_parts and not cpi_html_parts:
        return

    inner_html = "".join(fx_html_parts + cpi_html_parts)

    st.html(f"""
    <div style="background:linear-gradient(135deg,#f8fafc,#f0f4ff);
                border:1px solid #c7d2fe;border-radius:16px;
                padding:16px 20px;margin:8px 0 16px;
                font-family:'Inter',sans-serif">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="font-size:16px">🏢</span>
            <span style="font-size:13px;font-weight:700;color:#1e293b">
                {company} 맞춤 영향 분석
            </span>
            <span style="font-size:10px;color:#94a3b8;margin-left:auto">
                매출 {annual_rev:,.0f}억 기준
            </span>
        </div>
        {inner_html}
    </div>
    """)
