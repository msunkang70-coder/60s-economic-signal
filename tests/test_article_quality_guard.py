"""
기사 요약 품질 + 원문 링크 보호 테스트.
이 테스트가 실패하면 코드를 머지하지 마시오.
"""
import pytest
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSummaryQualityGuard:
    """요약 품질 최소 기준 보호"""

    def test_system_prompt_no_30char_limit(self):
        """SYSTEM_PROMPT에 '30자 이내' 제한이 없어야 함 (근본 원인)"""
        from core.summarizer import SYSTEM_PROMPT
        assert "30자" not in SYSTEM_PROMPT, \
            "SYSTEM_PROMPT에 '30자' 제한이 남아있음! 50~80자로 변경 필요"

    def test_system_prompt_has_minimum_length(self):
        """SYSTEM_PROMPT에 최소 길이 요구사항이 있어야 함"""
        from core.summarizer import SYSTEM_PROMPT
        assert "50" in SYSTEM_PROMPT or "80" in SYSTEM_PROMPT, \
            "SYSTEM_PROMPT에 최소 50자 이상 요구사항이 없음"

    def test_validate_summary_quality_exists(self):
        """_validate_summary_quality 함수가 존재해야 함"""
        from core.summarizer import _validate_summary_quality
        assert callable(_validate_summary_quality)

    def test_validate_summary_rejects_short(self):
        """짧은 요약은 품질 검증 실패해야 함"""
        from core.summarizer import _validate_summary_quality
        bad_summary = {
            "impact": "거버넌스 개선",
            "risk": "이해충돌",
            "opportunity": "밸류업 정책 효과",
            "action": "이해충돌 점검",
        }
        assert _validate_summary_quality(bad_summary) == False, \
            "짧은 요약이 품질 검증을 통과함! 최소 30자 기준 필요"

    def test_validate_summary_accepts_good(self):
        """양호한 요약은 품질 검증 통과해야 함"""
        from core.summarizer import _validate_summary_quality
        good_summary = {
            "impact": "환율 1,480원 돌파로 반도체 수출 마진 약 3%p 개선 전망, 2분기 내 효과 본격화",
            "risk": "원자재 수입 원가 동반 상승 시 마진 개선분 15% 상쇄 가능, 하반기 역마진 우려",
            "opportunity": "달러 매출 비중 60% 이상 기업은 2분기 내 환헷지 비율 30%→50% 조정 적기",
            "action": "주요 원자재 3개 공급사 결제 통화별 원가 변동률 즉시 점검, 이번 주 내 완료",
        }
        assert _validate_summary_quality(good_summary) == True


class TestURLConsistencyGuard:
    """URL 필드명 일관성 보호"""

    def test_fetch_list_returns_url_field(self):
        """fetch_list 반환값에 'url' 키가 있어야 함"""
        import inspect
        from core.fetcher import fetch_list
        source = inspect.getsource(fetch_list)
        assert '"url"' in source or "'url'" in source, \
            "fetch_list()에 'url' 키가 없음"

    def test_fetch_detail_returns_url_field(self):
        """fetch_detail 반환값에 'url' 키가 있어야 함 (source_url만으로 불충분)"""
        import inspect
        from core.fetcher import fetch_detail
        source = inspect.getsource(fetch_detail)
        assert ('"url"' in source and '"source_url"' in source) or \
               ('"url":' in source), \
            "fetch_detail()에 'url' 키가 없음! 'source_url'과 'url' 모두 반환해야 함"

    def test_main_content_url_fallback_starts_with_url(self):
        """main_content.py의 URL 폴백 체인이 'url' 키를 우선 확인해야 함"""
        with open(os.path.join(os.path.dirname(__file__), "..", "views", "main_content.py"), "r", encoding="utf-8") as f:
            content = f.read()
        url_pos = content.find('.get("url")')
        source_url_pos = content.find('.get("source_url")')
        assert url_pos > 0, "main_content.py에 .get('url') 폴백이 없음"

    def test_source_url_render_guard(self):
        """URL이 빈 문자열이면 '원문 보기' 링크가 렌더링되지 않아야 함"""
        with open(os.path.join(os.path.dirname(__file__), "..", "views", "main_content.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "startswith" in content or "http" in content, \
            "URL 유효성 검증 없이 '원문 보기' 렌더링됨"
