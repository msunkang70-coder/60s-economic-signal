"""
views/benchmark_widget.py
경쟁사 벤치마킹 비교 카드 위젯 — Plotly 바차트 + 인사이트.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

from core.benchmark_engine import BenchmarkResult, calculate_benchmark

# ── 디자인 상수 (기존 디자인 시스템 통일) ────────────────────
_PRIMARY = "#5B5FEE"
_BG_CARD = "#ffffff"
_BORDER = "#E2E8F0"
_TEXT_DARK = "#1E1B4B"
_TEXT_MUTED = "#64748b"
_FONT = "'Inter', sans-serif"

_POSITION_COLORS = {
    "above": "#22c55e",
    "below": "#ef4444",
    "inline": "#64748b",
}
_POSITION_LABELS = {
    "above": "업종 상위",
    "below": "업종 하위",
    "inline": "업종 평균",
}


# ── public API ───────────────────────────────────────────────

def render_benchmark_card(
    result: BenchmarkResult | None = None,
    macro_data: dict | None = None,
    company_profile: dict | None = None,
    industry_key: str | None = None,
) -> None:
    """벤치마킹 비교 카드 렌더링.

    Parameters
    ----------
    result : BenchmarkResult | None
        benchmark_engine.calculate_benchmark() 결과.
        None이면 company_profile + industry_key로 자동 생성 시도.
    macro_data : dict | None
        data/macro.json 데이터 (현재 환율 등 표시용)
    company_profile : dict | None
        기업 프로파일. 미입력 시 안내 메시지 표시.
    industry_key : str | None
        산업 키 (result가 None일 때 자동 생성용)
    """
    # 프로필 미입력 가드
    if result is None and not company_profile:
        render_benchmark_placeholder()
        return

    # 프로파일 핵심 필드가 모두 기본값(0/빈값)이면 안내만 표시
    if company_profile and all(
        company_profile.get(k, 0) in (0, 0.0, "", "설정 안 함", None)
        for k in ["export_ratio_pct", "dollar_payment_ratio_pct", "annual_revenue_100m"]
    ):
        render_benchmark_placeholder()
        return

    # result가 없으면 자동 생성
    if result is None and company_profile:
        result = calculate_benchmark(
            company_profile, industry_key or "일반", use_dart=False,
        )

    from ui.components import section_header

    section_header(
        "경쟁사 벤치마킹",
        subtitle=f"{result.industry_label} 업종 평균 대비 귀사 포지션",
        number="",
    )

    # 데이터 소스 배지
    if result.data_source == "dart":
        source_badge = (
            f'<span style="background:#f0fdf4;color:#16a34a;font-size:10px;'
            f'font-weight:700;padding:2px 8px;border-radius:8px;'
            f'border:1px solid #86efac">DART API · {result.peer_count}개사</span>'
        )
    else:
        source_badge = (
            '<span style="background:#eff6ff;color:#2563eb;font-size:10px;'
            'font-weight:700;padding:2px 8px;border-radius:8px;'
            'border:1px solid #93c5fd">업종 추정치</span>'
        )

    # 종합 인사이트 카드
    st.html(f"""
    <div style="background:{_BG_CARD};border:1px solid {_BORDER};border-radius:14px;
                padding:20px 24px;font-family:{_FONT};margin-bottom:16px;
                box-shadow:0 1px 4px rgba(0,0,0,0.05)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <span style="font-size:15px;font-weight:800;color:{_TEXT_DARK}">
          {result.company_name} vs {result.industry_label}
        </span>
        {source_badge}
      </div>
      <div style="font-size:13px;color:#475569;line-height:1.7">
        {result.summary_insight}
      </div>
    </div>
    """)

    # Plotly 바차트
    _render_comparison_chart(result)

    # 축별 인사이트 카드
    _render_axis_insights(result)

    # 현재 환율 컨텍스트 (macro_data 있을 때)
    if macro_data:
        _render_macro_context(macro_data, result)


def render_benchmark_placeholder() -> None:
    """DART API 미설정 또는 기업 프로파일 미입력 시 플레이스홀더."""
    st.html(f"""
    <div style="background:#eff6ff;border:1px solid #93c5fd;border-left:4px solid #3b82f6;
                border-radius:10px;padding:20px 24px;font-family:{_FONT};margin-bottom:16px">
      <div style="font-size:14px;font-weight:700;color:#1e40af;margin-bottom:6px">
        📊 경쟁사 벤치마킹
      </div>
      <div style="font-size:13px;color:#475569;line-height:1.6">
        기업 프로파일을 입력하면 업종 평균 대비 귀사의 포지션을 분석합니다.<br>
        DART API 키를 설정하면 실제 상장사 재무 데이터 기반 비교가 가능합니다.
      </div>
    </div>
    """)


# ── 내부 렌더링 ──────────────────────────────────────────────

def _render_comparison_chart(result: BenchmarkResult) -> None:
    """3축 비교 수평 바차트."""
    if not result.axes:
        return

    labels = [a.label for a in result.axes]
    company_vals = [a.company_value for a in result.axes]
    industry_vals = [a.industry_avg for a in result.axes]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        y=labels,
        x=company_vals,
        name=result.company_name or "귀사",
        orientation="h",
        marker_color=_PRIMARY,
        text=[f"{v:.1f}%" for v in company_vals],
        textposition="outside",
        textfont=dict(size=12, color=_TEXT_DARK),
    ))

    fig.add_trace(go.Bar(
        y=labels,
        x=industry_vals,
        name=f"{result.industry_label} 평균",
        orientation="h",
        marker_color="#CBD5E1",
        text=[f"{v:.1f}%" for v in industry_vals],
        textposition="outside",
        textfont=dict(size=12, color=_TEXT_MUTED),
    ))

    fig.update_layout(
        barmode="group",
        height=220,
        margin=dict(l=0, r=40, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(
            orientation="h", y=1.15, x=0,
            font=dict(size=12),
        ),
        xaxis=dict(showgrid=True, gridcolor="#f1f5f9", zeroline=False),
        yaxis=dict(autorange="reversed"),
        font=dict(family="Inter, sans-serif"),
    )

    st.plotly_chart(fig, use_container_width=True, key="agent5_benchmark_chart")


def _render_axis_insights(result: BenchmarkResult) -> None:
    """축별 인사이트 카드 그리드."""
    if not result.axes:
        return

    cards_html = ""
    for axis in result.axes:
        color = _POSITION_COLORS.get(axis.position, _TEXT_MUTED)
        pos_label = _POSITION_LABELS.get(axis.position, "—")
        diff_sign = "+" if axis.diff_pct > 0 else ""

        cards_html += f"""
        <div style="background:{_BG_CARD};border:1px solid {_BORDER};
                    border-top:3px solid {color};border-radius:12px;
                    padding:16px;box-shadow:0 1px 4px rgba(0,0,0,0.05)">
          <div style="font-size:12px;font-weight:600;color:{_TEXT_MUTED};margin-bottom:6px">
            {axis.label}
          </div>
          <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
            <span style="font-size:24px;font-weight:800;color:{_TEXT_DARK}">{axis.company_value:.1f}%</span>
            <span style="font-size:12px;font-weight:600;color:{color}">
              {diff_sign}{axis.diff_pct:.1f}%p · {pos_label}
            </span>
          </div>
          <div style="font-size:12px;color:#475569;line-height:1.5">
            {axis.insight}
          </div>
        </div>"""

    st.html(f"""
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;
                font-family:{_FONT};margin-bottom:16px">
      {cards_html}
    </div>
    """)


def _render_macro_context(macro_data: dict, result: BenchmarkResult) -> None:
    """현재 환율 등 매크로 컨텍스트 표시."""
    fx = macro_data.get("환율(원/$)", {})
    fx_value = fx.get("value", "—")
    fx_trend = fx.get("trend", "→")

    # 환율 민감도 축 찾기
    fx_axis = next((a for a in result.axes if a.axis == "fx_sensitivity"), None)
    if not fx_axis:
        return

    trend_color = "#dc2626" if fx_trend == "▲" else "#2563eb" if fx_trend == "▼" else _TEXT_MUTED

    st.html(f"""
    <div style="background:#fafafe;border:1px solid {_BORDER};border-radius:10px;
                padding:14px 20px;font-family:{_FONT};margin-bottom:12px;
                display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div>
        <span style="font-size:11px;color:{_TEXT_MUTED};font-weight:600">현재 환율</span>
        <div style="font-size:20px;font-weight:800;color:{_TEXT_DARK}">{fx_value}
          <span style="color:{trend_color};font-size:16px">{fx_trend}</span>
        </div>
      </div>
      <div style="flex:1;font-size:12px;color:#475569;line-height:1.5">
        귀사 환율 민감도({fx_axis.company_value:.1f}%)가 업종 평균({fx_axis.industry_avg:.1f}%)
        {'보다 높아' if fx_axis.position == 'above' else '보다 낮아' if fx_axis.position == 'below' else '과 유사하여'}
        현재 환율 {fx_trend} 추세에
        {'유리' if (fx_axis.position == 'above' and fx_trend == '▲') else '주의가 필요'}합니다.
      </div>
    </div>
    """)
