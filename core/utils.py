"""
core/utils.py
공통 유틸리티 함수 모음
"""

import functools
import logging
import re
import hashlib

logger = logging.getLogger("60sec_signal")


def safe_execute(default=None, log_prefix=""):
    """데코레이터: 예외 발생 시 default 반환 + 로깅."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"[{log_prefix or func.__name__}] {type(e).__name__}: {e}")
                return default
        return wrapper
    return decorator


def safe_float(value, default=0.0) -> float:
    """안전한 float 변환."""
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return default


def safe_json_load(path, default=None):
    """안전한 JSON 파일 로드."""
    import json
    import pathlib as _pathlib
    try:
        return json.loads(_pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def clean_text(text: str) -> str:
    """
    연속 줄바꿈(3개 이상 → 2개)과 탭/공백 중복을 정리한다.
    BeautifulSoup get_text() 결과를 사람이 읽기 좋게 만들 때 사용.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def single_line(text: str) -> str:
    """
    모든 공백 문자(줄바꿈 포함)를 단일 공백으로 변환한다.
    스크립트 문장 생성 시 줄바꿈 제거에 사용.
    """
    return re.sub(r"\s+", " ", text).strip()


def compute_hash(*parts: str) -> str:
    """
    여러 문자열을 '|'로 연결한 뒤 SHA-256 해시(hex)를 반환한다.
    SQLite 중복 저장 방지용 content_hash 생성에 사용.

    예시:
        compute_hash("2026-02", "page_title", "url1", "url2")
        → "a3f2c1d..." (64자 hex 문자열)
    """
    combined = "|".join(str(p) for p in parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
