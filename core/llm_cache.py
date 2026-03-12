"""
core/llm_cache.py
Dict 기반 인메모리 캐시 — TTL + LRU eviction.
+ 파일 기반 LLM 요약 캐시 (data/llm_cache.json, max 200, LRU)

용도: LLM 분석 결과 캐싱으로 중복 API 호출 방지.
"""

from __future__ import annotations

import json
import pathlib
import time
from collections import OrderedDict
from typing import Any

_DEFAULT_TTL = 6 * 3600   # 6시간
_MAX_ENTRIES = 500

_ROOT = pathlib.Path(__file__).parent.parent
_LLM_CACHE_PATH = _ROOT / "data" / "llm_cache.json"
_LLM_CACHE_MAX = 200


class LLMCache:
    """TTL + LRU 기반 인메모리 캐시.

    Parameters
    ----------
    ttl : int
        캐시 항목 유효 기간(초). 기본 6시간.
    max_entries : int
        최대 항목 수. 초과 시 가장 오래 사용되지 않은 항목부터 제거.
    """

    def __init__(self, ttl: int = _DEFAULT_TTL, max_entries: int = _MAX_ENTRIES):
        self._ttl = ttl
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    # ── public API ───────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """캐시 조회. 히트 시 LRU 갱신, 만료 시 제거 후 None."""
        if key not in self._store:
            self._misses += 1
            return None
        ts, value = self._store[key]
        if time.time() - ts > self._ttl:
            del self._store[key]
            self._misses += 1
            return None
        # LRU: 최근 사용으로 이동
        self._store.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        """캐시 저장. max_entries 초과 시 LRU 제거."""
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (time.time(), value)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def make_key(self, doc_id: str, analysis_type: str) -> str:
        """doc_id + analysis_type 으로 캐시 키 생성."""
        return f"{doc_id}::{analysis_type}"

    def invalidate(self, key: str) -> bool:
        """특정 키 제거. 제거 성공 시 True."""
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        """전체 캐시 초기화."""
        self._store.clear()

    def size(self) -> int:
        """현재 저장된 항목 수 (만료 항목 포함)."""
        return len(self._store)

    def purge_expired(self) -> int:
        """만료된 항목을 일괄 제거하고 제거 건수를 반환."""
        now = time.time()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        return len(expired)

    def batch_purge(self, keys: list[str] | None = None) -> int:
        """지정된 키들을 일괄 제거. keys=None이면 만료 항목만 제거.

        Returns:
            제거된 항목 수.
        """
        if keys is None:
            return self.purge_expired()
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    def get_cache_health(self) -> dict:
        """캐시 건강 상태를 반환.

        Returns:
            {"size", "max", "ttl", "hits", "misses", "hit_rate",
             "valid", "expired", "utilization"}
        """
        now = time.time()
        valid = sum(1 for ts, _ in self._store.values() if now - ts <= self._ttl)
        expired = len(self._store) - valid
        total_requests = self._hits + self._misses
        return {
            "size": len(self._store),
            "max": self._max,
            "ttl": self._ttl,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total_requests, 4) if total_requests > 0 else 0.0,
            "valid": valid,
            "expired": expired,
            "utilization": round(len(self._store) / self._max, 4) if self._max > 0 else 0.0,
        }


# ── 모듈 수준 싱글턴 ─────────────────────────────────────────
_global_cache = LLMCache()


def get_cache() -> LLMCache:
    """전역 LLM 캐시 인스턴스를 반환한다."""
    return _global_cache


# ══════════════════════════════════════════════════════════
# 파일 기반 LLM 요약 캐시 (data/llm_cache.json)
# ══════════════════════════════════════════════════════════

def _load_llm_file() -> dict:
    """파일에서 LLM 요약 캐시를 로드."""
    if _LLM_CACHE_PATH.exists():
        try:
            data = json.loads(_LLM_CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_llm_file(data: dict) -> None:
    """LLM 요약 캐시를 파일에 저장. LRU eviction 적용."""
    # max 초과 시 가장 오래된 항목 제거 (accessed_at 기준)
    if len(data) > _LLM_CACHE_MAX:
        sorted_keys = sorted(
            data.keys(),
            key=lambda k: data[k].get("accessed_at", data[k].get("cached_at", 0)),
        )
        for k in sorted_keys[: len(data) - _LLM_CACHE_MAX]:
            del data[k]
    _LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LLM_CACHE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_llm_summary(doc_id: str) -> dict | None:
    """파일 캐시에서 LLM 요약 조회.

    Returns:
        {"summary": ..., "source": ..., "cached_at": ...} 또는 None
    """
    data = _load_llm_file()
    entry = data.get(doc_id)
    if entry is None:
        return None
    # LRU: accessed_at 갱신
    entry["accessed_at"] = time.time()
    data[doc_id] = entry
    try:
        _save_llm_file(data)
    except Exception:
        pass
    return entry


def set_llm_summary(doc_id: str, summary, source: str = "groq") -> None:
    """LLM 요약 결과를 파일 캐시에 저장.

    Args:
        doc_id: 문서 ID
        summary: 요약 결과 (str 또는 dict)
        source: 요약 출처 ("groq", "gemini", "rule" 등)
    """
    data = _load_llm_file()
    now = time.time()
    data[doc_id] = {
        "summary": summary,
        "source": source,
        "cached_at": now,
        "accessed_at": now,
    }
    _save_llm_file(data)
