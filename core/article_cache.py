"""
core/article_cache.py — 기사 상세 인메모리 캐시 (Phase 13 Agent 3)

dict 기반 LRU 캐시. doc_type별 TTL, 최대 100 엔트리.
싱글톤: get_cache()로 접근.
"""

import threading
import time
from collections import OrderedDict

_DEFAULT_TTL = 3 * 3600   # 3시간 (초)
_DEFAULT_MAX = 100

_TTL_MAP = {
    "news": 3600,       # 뉴스: 1시간
    "policy": 21600,    # 정책: 6시간
    "fail": 1800,       # 실패: 30분 (Fix D — 동일 URL 재시도 방지, 단기 보관)
    "default": 10800,   # 기본: 3시간
}


class ArticleCache:
    """Thread-safe LRU article detail cache with per-type TTL."""

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL,
        max_entries: int = _DEFAULT_MAX,
        ttl_map: dict[str, int] | None = None,
    ):
        self._ttl = ttl
        self._max = max_entries
        self._ttl_map = ttl_map or dict(_TTL_MAP)
        # store: doc_id -> (timestamp, doc_type, data)
        self._store: OrderedDict[str, tuple[float, str, dict]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _resolve_ttl(self, doc_type: str) -> int:
        """doc_type에 맞는 TTL 반환."""
        return self._ttl_map.get(doc_type, self._ttl_map.get("default", self._ttl))

    # ── public API ────────────────────────────────────

    def get(self, doc_id: str, doc_type: str = "default") -> dict | None:
        """캐시에서 doc_id로 조회. doc_type별 TTL 적용. 만료/미존재 시 None."""
        with self._lock:
            entry = self._store.get(doc_id)
            if entry is None:
                self._misses += 1
                return None
            ts, stored_type, data = entry
            ttl = self._resolve_ttl(stored_type)
            if time.time() - ts > ttl:
                del self._store[doc_id]
                self._misses += 1
                return None
            # LRU: 최근 접근을 끝으로 이동
            self._store.move_to_end(doc_id)
            self._hits += 1
            return data

    def set(self, doc_id: str, data: dict, doc_type: str = "default") -> None:
        """캐시에 저장. 최대 엔트리 초과 시 가장 오래된 항목 제거."""
        with self._lock:
            if doc_id in self._store:
                del self._store[doc_id]
            self._store[doc_id] = (time.time(), doc_type, data)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def has(self, doc_id: str) -> bool:
        """만료되지 않은 항목 존재 여부."""
        return self.get(doc_id) is not None

    def invalidate(self, doc_id: str) -> bool:
        """특정 항목 삭제. 삭제 성공 시 True."""
        with self._lock:
            if doc_id in self._store:
                del self._store[doc_id]
                return True
            return False

    def clear(self) -> None:
        """전체 캐시 초기화."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def size(self) -> int:
        """현재 저장된 (만료 미확인) 항목 수."""
        with self._lock:
            return len(self._store)

    def stats(self) -> dict:
        """캐시 상태 정보 (hit/miss 통계 포함)."""
        with self._lock:
            now = time.time()
            valid = 0
            for ts, dtype, _ in self._store.values():
                ttl = self._resolve_ttl(dtype)
                if now - ts <= ttl:
                    valid += 1
            total_requests = self._hits + self._misses
            return {
                "total": len(self._store),
                "valid": valid,
                "expired": len(self._store) - valid,
                "max": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total_requests, 4) if total_requests > 0 else 0.0,
            }

    def warm_cache(
        self,
        doc_ids: list[str],
        fetcher_fn=None,
        industry_key: str = "일반",
    ) -> int:
        """지정된 doc_id 목록을 사전 캐싱(warm-up).

        Args:
            doc_ids: 캐싱할 문서 ID 리스트.
            fetcher_fn: doc_id -> dict 반환 함수. None이면 빈 dict 저장.
            industry_key: doc_type으로 사용할 키.

        Returns:
            새로 캐싱된 항목 수.
        """
        warmed = 0
        for doc_id in doc_ids:
            # 이미 유효한 캐시가 있으면 skip
            if self.get(doc_id, industry_key) is not None:
                continue
            try:
                data = fetcher_fn(doc_id) if fetcher_fn else {}
            except Exception:
                data = {}
            if data is not None:
                self.set(doc_id, data if isinstance(data, dict) else {}, industry_key)
                warmed += 1
        return warmed

    def get_cache_health(self) -> dict:
        """캐시 건강 상태 반환 (stats 확장).

        Returns:
            stats() 결과 + utilization, ttl_map 정보.
        """
        base = self.stats()
        base["utilization"] = round(base["total"] / self._max, 4) if self._max > 0 else 0.0
        base["ttl_map"] = dict(self._ttl_map)
        return base


# ── 싱글톤 ────────────────────────────────────────────

_instance: ArticleCache | None = None
_instance_lock = threading.Lock()


def get_cache() -> ArticleCache:
    """싱글톤 ArticleCache 인스턴스 반환."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ArticleCache()
    return _instance
