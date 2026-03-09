"""
core/content_manager.py
콘텐츠 생성 이력 관리 — data/content_db.json 기반

설계 원칙:
  - Git-Friendly  : JSON 파일 → GitHub Actions 자동 커밋 가능 (SQLite 불가)
  - 멱등(Idempotent): content_id(=YYYYMMDD) 기준 upsert — 같은 날 재실행 시 갱신
  - 원자적 쓰기   : tmp 파일 → fsync → os.replace 로 파일 손상 방지
  - 의존성 최소화 : 표준 라이브러리만 사용 (json, pathlib, datetime, os, tempfile)

저장 위치: <project_root>/data/content_db.json

스키마 (레코드 1건):
  {
    "content_id":   "20260306",          # YYYYMMDD — 기본 키
    "date":         "2026-03-06",        # 사람이 읽기 쉬운 날짜
    "generated_at": "2026-03-06 09:12:34",
    "topic":        "경제",
    "macro_data":   "data/macro.json",   # 프로젝트 루트 기준 상대경로
    "script_path":  "outputs/output_script.txt",
    "srt_path":     "outputs/output_script.srt"
  }

사용 예:
    from core.content_manager import save_content_record, load_content_history

    # main.py에서 스크립트 생성 직후 호출
    record = save_content_record(
        topic="경제",
        macro_data_path="data/macro.json",
        script_path="outputs/output_script.txt",
        srt_path="outputs/output_script.srt",
    )

    # Streamlit app.py에서 이력 조회
    history = load_content_history(limit=20)
"""

import json
import os
import pathlib
import tempfile
from datetime import datetime
from typing import Optional

# ── 경로 설정 ──────────────────────────────────────────────────
# core/content_manager.py → 부모의 부모 = 프로젝트 루트
_ROOT   = pathlib.Path(__file__).parent.parent
DB_PATH = _ROOT / "data" / "content_db.json"


# ─────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────

def _to_rel(path) -> str:
    """
    절대경로 → 프로젝트 루트 기준 상대경로 문자열로 변환.
    이미 상대경로거나 None이면 그대로 처리한다.
    Windows 경로 구분자(\\)는 '/'로 정규화한다.
    """
    if not path:
        return ""
    try:
        rel = pathlib.Path(path).relative_to(_ROOT)
    except ValueError:
        # relative_to 실패 = 이미 상대경로이거나 다른 루트
        rel = pathlib.Path(path)
    # POSIX 슬래시로 정규화 (크로스플랫폼 일관성)
    return rel.as_posix()


def _load_raw() -> list:
    """
    content_db.json을 읽어 레코드 리스트로 반환한다.
    파일이 없거나 JSON 파싱에 실패하면 빈 리스트를 반환한다.
    """
    if not DB_PATH.exists():
        return []
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_atomic(records: list) -> None:
    """
    records 리스트를 content_db.json에 원자적으로 저장한다.

    순서:
      1. 같은 디렉터리에 임시 파일(.tmp) 생성
      2. JSON 직렬화 후 fsync (OS 버퍼 강제 플러시)
      3. os.replace — POSIX에서 원자적 rename 보장

    파일 손상 시나리오(쓰기 도중 프로세스 종료 등)에서
    기존 content_db.json을 안전하게 보존한다.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(records, ensure_ascii=False, indent=2)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=DB_PATH.parent,
        prefix=".content_db_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())   # OS 버퍼 → 디스크 강제 플러시
        os.replace(tmp_path, DB_PATH)   # 원자적 rename
    except Exception:
        # 임시 파일 정리 후 예외 재전파
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────

def save_content_record(
    topic: str = "경제",
    macro_data_path=None,
    script_path=None,
    srt_path=None,
    extra: Optional[dict] = None,
) -> dict:
    """
    콘텐츠 생성 결과를 content_db.json에 저장(upsert)한다.

    content_id = 실행 날짜 YYYYMMDD.
    같은 날 재실행 시 기존 레코드를 덮어쓴다(upsert).
    새 레코드는 리스트의 맨 앞(인덱스 0)에 삽입하여
    load_content_history()에서 최신순이 앞에 오도록 한다.

    Args:
        topic          : 콘텐츠 토픽 레이블 (기본값: "경제")
        macro_data_path: macro.json 경로 (str | Path | None)
        script_path    : output_script.txt 경로 (str | Path | None)
        srt_path       : output_script.srt 경로 (str | Path | None)
        extra          : 추가 메타데이터 dict — 확장 필드 자유롭게 추가 가능

    Returns:
        저장된 record dict

    Raises:
        OSError: data/ 디렉터리 생성 또는 파일 쓰기 실패
    """
    now        = datetime.now()
    content_id = now.strftime("%Y%m%d")

    record: dict = {
        "content_id":   content_id,
        "date":         now.strftime("%Y-%m-%d"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "topic":        topic,
        "macro_data":   _to_rel(macro_data_path),
        "script_path":  _to_rel(script_path),
        "srt_path":     _to_rel(srt_path),
    }
    if extra:
        # extra 키가 기본 키를 덮어쓰지 못하도록 뒤에 병합
        for k, v in extra.items():
            if k not in record:
                record[k] = v

    records = _load_raw()

    # upsert: 동일 content_id 교체, 없으면 맨 앞에 삽입
    idx = next(
        (i for i, r in enumerate(records) if r.get("content_id") == content_id),
        None,
    )
    if idx is not None:
        records[idx] = record      # 기존 레코드 갱신
    else:
        records.insert(0, record)  # 신규 레코드를 앞에 삽입

    _write_atomic(records)
    print(
        f"[content_manager] ✓ content_db.json 저장 완료 "
        f"→ content_id={content_id}  총 {len(records)}건"
    )
    return record


def load_content_history(limit: int = 50) -> list:
    """
    content_db.json에서 최근 limit건의 콘텐츠 이력을 반환한다.

    generated_at 내림차순(최신 → 과거)으로 정렬된다.
    경로 필드(script_path, srt_path, macro_data)는
    프로젝트 루트 기준 상대경로(POSIX 슬래시)다.

    Args:
        limit: 최대 반환 건수 (기본값 50)

    Returns:
        [{"content_id", "date", "topic", "script_path", ...}, ...]
    """
    records = _load_raw()
    # generated_at 문자열 내림차순 (ISO 형식이므로 문자열 정렬 == 시간 정렬)
    records.sort(key=lambda r: r.get("generated_at", ""), reverse=True)
    return records[:limit]


def get_content_by_id(content_id: str) -> Optional[dict]:
    """
    content_id로 특정 레코드를 조회한다.

    Args:
        content_id: YYYYMMDD 형식 문자열

    Returns:
        해당 record dict 또는 None
    """
    for r in _load_raw():
        if r.get("content_id") == content_id:
            return r
    return None


def delete_content_record(content_id: str) -> bool:
    """
    특정 content_id 레코드를 삭제한다.

    Args:
        content_id: YYYYMMDD 형식 문자열

    Returns:
        True(삭제 성공) | False(해당 id 없음)
    """
    records = _load_raw()
    new_records = [r for r in records if r.get("content_id") != content_id]
    if len(new_records) == len(records):
        return False   # 해당 id 없음
    _write_atomic(new_records)
    print(f"[content_manager] 삭제 완료 → content_id={content_id}")
    return True
