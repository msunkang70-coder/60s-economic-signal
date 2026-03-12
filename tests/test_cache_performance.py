"""
tests/test_cache_performance.py
Agent 5 — LLMCache / ArticleCache / auto_pipeline 성능 & 파이프라인 테스트 (12+)
"""

import json
import time
from datetime import datetime
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────
# 1. LLMCache — hit/miss 카운터 & batch_purge & get_cache_health
# ─────────────────────────────────────────────────────────────

class TestLLMCacheHitMiss:
    def _make(self, **kw):
        from core.llm_cache import LLMCache
        return LLMCache(**kw)

    def test_hit_counter_increments(self):
        c = self._make()
        c.set("k1", "v1")
        c.get("k1")
        assert c._hits == 1 and c._misses == 0

    def test_miss_counter_increments(self):
        c = self._make()
        c.get("nokey")
        assert c._misses == 1 and c._hits == 0

    def test_expired_counts_as_miss(self):
        c = self._make(ttl=0)
        c.set("k1", "v1")
        time.sleep(0.01)
        assert c.get("k1") is None
        assert c._misses == 1

    def test_batch_purge_specific_keys(self):
        c = self._make()
        c.set("a", 1); c.set("b", 2); c.set("c", 3)
        removed = c.batch_purge(["a", "c", "nonexist"])
        assert removed == 2
        assert c.size() == 1

    def test_batch_purge_none_removes_expired(self):
        c = self._make(ttl=0)
        c.set("a", 1); c.set("b", 2)
        time.sleep(0.01)
        removed = c.batch_purge(None)
        assert removed == 2
        assert c.size() == 0

    def test_get_cache_health_structure(self):
        c = self._make()
        c.set("x", 10)
        c.get("x")
        c.get("miss")
        h = c.get_cache_health()
        assert h["size"] == 1
        assert h["hits"] == 1
        assert h["misses"] == 1
        assert h["hit_rate"] == 0.5
        assert "utilization" in h
        assert "valid" in h and "expired" in h

    def test_get_cache_health_zero_requests(self):
        c = self._make()
        h = c.get_cache_health()
        assert h["hit_rate"] == 0.0


# ─────────────────────────────────────────────────────────────
# 2. ArticleCache — warm_cache & get_cache_health
# ─────────────────────────────────────────────────────────────

class TestArticleCacheWarm:
    def _make(self):
        from core.article_cache import ArticleCache
        return ArticleCache()

    def test_warm_cache_basic(self):
        c = self._make()
        warmed = c.warm_cache(["d1", "d2", "d3"])
        assert warmed == 3
        assert c.size() == 3

    def test_warm_cache_skips_existing(self):
        c = self._make()
        c.set("d1", {"title": "cached"}, "일반")
        warmed = c.warm_cache(["d1", "d2"])
        assert warmed == 1  # d1 skipped

    def test_warm_cache_with_fetcher(self):
        c = self._make()
        fetcher = lambda doc_id: {"id": doc_id, "body": "content"}
        warmed = c.warm_cache(["a", "b"], fetcher_fn=fetcher)
        assert warmed == 2
        data = c.get("a", "일반")
        assert data["id"] == "a"

    def test_warm_cache_fetcher_error_handled(self):
        c = self._make()
        def bad_fetcher(doc_id):
            raise RuntimeError("fetch failed")
        warmed = c.warm_cache(["x"], fetcher_fn=bad_fetcher)
        assert warmed == 1  # stores empty dict on error

    def test_get_cache_health(self):
        c = self._make()
        c.set("d1", {"x": 1})
        h = c.get_cache_health()
        assert "utilization" in h
        assert "ttl_map" in h
        assert h["total"] == 1


# ─────────────────────────────────────────────────────────────
# 3. _CircuitBreaker
# ─────────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def _make(self, **kw):
        from core.auto_pipeline import _CircuitBreaker
        return _CircuitBreaker(**kw)

    def test_starts_closed(self):
        cb = self._make()
        assert cb.state == "CLOSED"
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        cb = self._make(failure_threshold=2)
        cb.record_failure()
        assert cb.state == "CLOSED"
        cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.allow_request() is False

    def test_half_open_after_timeout(self):
        cb = self._make(failure_threshold=1, reset_timeout=9999)
        cb.record_failure()
        assert cb.state == "OPEN"
        # Simulate timeout elapsed
        cb._last_failure_time = time.time() - 10000
        assert cb.state == "HALF_OPEN"
        assert cb.allow_request() is True

    def test_success_resets(self):
        cb = self._make(failure_threshold=1)
        cb.record_failure()
        assert cb.state == "OPEN"
        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb._failures == 0

    def test_to_dict(self):
        cb = self._make()
        d = cb.to_dict()
        assert "state" in d and "failures" in d


# ─────────────────────────────────────────────────────────────
# 4. run_health_check & get_pipeline_metrics
# ─────────────────────────────────────────────────────────────

class TestPipelineHealthMetrics:
    def test_run_health_check_ok(self):
        from core.auto_pipeline import run_health_check
        result = run_health_check()
        assert result["status"] in ("ok", "degraded", "down")
        assert "checks" in result

    def test_get_pipeline_metrics_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.auto_pipeline._PIPELINE_LOG_PATH",
                            tmp_path / "pipeline.jsonl")
        from core.auto_pipeline import get_pipeline_metrics
        m = get_pipeline_metrics(days=7)
        assert m["total_events"] == 0
        assert m["days"] == 7

    def test_get_pipeline_metrics_with_data(self, tmp_path, monkeypatch):
        log_path = tmp_path / "pipeline.jsonl"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            json.dumps({"timestamp": now_str, "event": "pipeline_start"}),
            json.dumps({"timestamp": now_str, "event": "pipeline_complete",
                        "detail": {"indicators": 7}}),
        ]
        log_path.write_text("\n".join(lines), encoding="utf-8")
        monkeypatch.setattr("core.auto_pipeline._PIPELINE_LOG_PATH", log_path)

        from core.auto_pipeline import get_pipeline_metrics
        m = get_pipeline_metrics(days=7)
        assert m["total_events"] == 2
        assert m["events"].get("pipeline_complete") == 1
        assert m["last_success"] == now_str
