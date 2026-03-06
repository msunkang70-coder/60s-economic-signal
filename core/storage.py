"""
core/storage.py
SQLite 기반 히스토리 저장/조회 담당.

DB 파일: <project_root>/data/history.db (실행 시 자동 생성)
테이블: runs
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

# ──────────────────────────────────────────────────────
# DB 경로 설정
# ──────────────────────────────────────────────────────
# 이 파일(core/storage.py)의 부모 디렉터리(project root) 기준으로 경로 구성
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_ROOT_DIR, "data", "history.db")


# ──────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    """
    data/ 디렉터리를 자동 생성하고 SQLite 연결을 반환한다.
    Row 팩토리를 설정하여 dict처럼 접근할 수 있게 한다.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────────────
# 1. DB 초기화
# ──────────────────────────────────────────────────────
def init_db() -> None:
    """
    테이블과 인덱스를 생성한다 (IF NOT EXISTS이므로 멱등 호출 가능).
    Streamlit 앱 시작 시 한 번 호출하면 된다.

    테이블 구조:
        id                  PK, AUTOINCREMENT
        created_at          TEXT  생성 일시 (YYYY-MM-DD HH:MM:SS)
        month_key           TEXT  년월 (YYYY-MM)
        page_title          TEXT  KDI 목록 페이지 제목
        source_url          TEXT  수집 대상 URL
        top_n               INT   수집한 기사 수
        article_titles_json TEXT  기사 제목 JSON 배열
        article_urls_json   TEXT  기사 URL JSON 배열
        content_hash        TEXT  중복 방지용 SHA-256 해시
        script_text         TEXT  생성된 스크립트 전문
    """
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT    NOT NULL,
                month_key           TEXT    NOT NULL,
                page_title          TEXT    NOT NULL,
                source_url          TEXT    NOT NULL,
                top_n               INTEGER NOT NULL,
                article_titles_json TEXT    NOT NULL,
                article_urls_json   TEXT    NOT NULL,
                content_hash        TEXT    NOT NULL,
                script_text         TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_runs_month_key
            ON runs (month_key)
        """)
        conn.commit()


# ──────────────────────────────────────────────────────
# 2. 결과 저장
# ──────────────────────────────────────────────────────
def save_run(
    month_key: str,
    page_title: str,
    source_url: str,
    top_n: int,
    articles: list,
    content_hash: str,
    script_text: str,
) -> tuple:
    """
    스크립트 생성 결과를 DB에 저장한다.

    중복 방지:
        (month_key, page_title, top_n, content_hash) 4개 컬럼이
        모두 동일한 행이 이미 존재하면 저장하지 않고 기존 ID를 반환한다.

    Args:
        month_key:     "YYYY-MM" 형식 문자열
        page_title:    목록 페이지 제목
        source_url:    수집 대상 URL
        top_n:         수집 기사 수
        articles:      [{"title": str, "url": str}, ...] 기사 목록
        content_hash:  중복 방지용 SHA-256 해시 (compute_hash() 결과)
        script_text:   생성된 스크립트 문자열

    Returns:
        (run_id: int, is_new: bool)
        is_new=True  → 새로 저장됨
        is_new=False → 중복으로 기존 행의 id 반환
    """
    with _connect() as conn:
        # ── 중복 체크 ──────────────────────────────
        dup = conn.execute(
            """
            SELECT id FROM runs
            WHERE month_key = ? AND page_title = ? AND top_n = ? AND content_hash = ?
            """,
            (month_key, page_title, top_n, content_hash),
        ).fetchone()

        if dup:
            return (dup["id"], False)  # 중복 → 기존 ID, is_new=False

        # ── 저장 ───────────────────────────────────
        titles_json = json.dumps(
            [a.get("title", "") for a in articles], ensure_ascii=False
        )
        urls_json = json.dumps(
            [a.get("url", "") for a in articles], ensure_ascii=False
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute(
            """
            INSERT INTO runs
                (created_at, month_key, page_title, source_url, top_n,
                 article_titles_json, article_urls_json, content_hash, script_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, month_key, page_title, source_url, top_n,
                titles_json, urls_json, content_hash, script_text,
            ),
        )
        conn.commit()
        return (cur.lastrowid, True)  # 신규 저장, is_new=True


# ──────────────────────────────────────────────────────
# 3. 히스토리 조회
# ──────────────────────────────────────────────────────
def get_runs(
    month_key: Optional[str] = None,
    keyword: Optional[str] = None,
) -> list[dict]:
    """
    저장된 실행 히스토리를 최신순으로 최대 100건 반환한다.

    Args:
        month_key: 특정 년월로 필터 ("YYYY-MM"). None이면 전체
        keyword:   페이지 제목 / 스크립트 / 기사 제목 내 키워드 검색.
                   None이면 전체

    Returns:
        dict 리스트 (sqlite3.Row → dict 변환)
    """
    with _connect() as conn:
        query = "SELECT * FROM runs WHERE 1=1"
        params: list = []

        if month_key:
            query += " AND month_key = ?"
            params.append(month_key)

        if keyword:
            like = f"%{keyword}%"
            query += (
                " AND (page_title LIKE ?"
                " OR script_text LIKE ?"
                " OR article_titles_json LIKE ?)"
            )
            params += [like, like, like]

        query += " ORDER BY id DESC LIMIT 100"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_run_by_id(run_id: int) -> Optional[dict]:
    """
    특정 ID의 실행 결과를 반환한다.

    Args:
        run_id: runs 테이블의 id

    Returns:
        dict 또는 None (해당 id가 없는 경우)
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_month_keys() -> list[str]:
    """
    저장된 모든 고유 month_key("YYYY-MM") 목록을 최신순으로 반환한다.
    히스토리 탭의 월별 필터 selectbox에 사용된다.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT month_key FROM runs ORDER BY month_key DESC"
        ).fetchall()
        return [r[0] for r in rows]


def delete_run(run_id: int) -> bool:
    """
    특정 ID의 실행 결과를 삭제한다.

    Returns:
        True (삭제 성공) 또는 False (해당 id 없음)
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
        return cur.rowcount > 0


def get_run_count() -> int:
    """저장된 전체 실행 건수를 반환한다."""
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        return row[0] if row else 0
