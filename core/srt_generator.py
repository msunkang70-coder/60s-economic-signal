"""
core/srt_generator.py
output_script.txt 의 시간 태그 → SRT 자막 파일 변환

지원 태그 형식:
    [0~5초 훅]
    [5~25초 핵심 이슈 3개]
    [25~45초 해석]
    [45~60초 개인/기업 시사점 + 클로징]

출력 예시 (outputs/output_script.srt):
    1
    00:00:00,000 --> 00:00:05,000
    이번 달 꼭 알아야 할 경제 핵심 이슈 3가지, 60초로 정리해드립니다!

    2
    00:00:05,000 --> 00:00:25,000
    첫째, 반도체 수출이 전월 대비 증가했습니다.
    둘째, 소비자물가는 2.1%를 유지했습니다.
    셋째, 기준금리는 동결됐습니다.

CLI:
    python -m core.srt_generator outputs/output_script.txt
"""

import pathlib
import re
import sys

# ── 상수 ──────────────────────────────────────────────────────
# [start~end초 제목] 패턴 — 괄호 안에 '~'와 '초' 가 있는 경우만 매칭
_TAG_RE = re.compile(r"\[(\d+)~(\d+)초[^\]]*\]")


# ─────────────────────────────────────────────────────────────
# 1. 초 → SRT 타임스탬프 변환
# ─────────────────────────────────────────────────────────────
def _to_srt_time(seconds: int) -> str:
    """
    정수 초 → 'HH:MM:SS,000' SRT 타임스탬프 문자열.

    예시:
        _to_srt_time(0)  → '00:00:00,000'
        _to_srt_time(60) → '00:01:00,000'
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d},000"


# ─────────────────────────────────────────────────────────────
# 2. 스크립트 텍스트 → 구간 목록 파싱
# ─────────────────────────────────────────────────────────────
def _parse_sections(text: str) -> list:
    """
    output_script.txt 텍스트에서 시간 구간과 본문을 추출한다.

    처리 규칙:
      - '※ 참고 기사' 이후 내용은 무시
      - 구분선(---) 줄은 제거
      - 빈 줄은 제거
      - 각 구간의 텍스트: 해당 태그 다음 줄 ~ 다음 태그 직전

    Returns:
        [(start_sec: int, end_sec: int, content: str), ...]
    """
    # ※ 참고 기사 이후 내용 제거
    cutoff = text.find("※ 참고 기사")
    body   = text[:cutoff] if cutoff != -1 else text

    matches = list(_TAG_RE.finditer(body))
    if not matches:
        return []

    sections = []
    for i, m in enumerate(matches):
        start_sec = int(m.group(1))
        end_sec   = int(m.group(2))

        # 이 태그 이후부터 다음 태그 직전까지
        text_start = m.end()
        text_end   = matches[i + 1].start() if i + 1 < len(matches) else len(body)

        raw = body[text_start:text_end]

        # 빈 줄 / 구분선 제거 → 본문 줄 수집
        lines = [
            ln.strip()
            for ln in raw.splitlines()
            if ln.strip() and not re.match(r"^-{3,}$", ln.strip())
        ]
        content = "\n".join(lines)
        sections.append((start_sec, end_sec, content))

    return sections


# ─────────────────────────────────────────────────────────────
# 3. SRT 파일 생성 (메인 함수)
# ─────────────────────────────────────────────────────────────
def generate_srt(script_path, output_path=None) -> str:
    """
    output_script.txt → output_script.srt 변환.

    Args:
        script_path: 입력 스크립트 파일 경로 (str | Path)
        output_path: 출력 SRT 경로 (None이면 script_path와 같은 디렉터리에 .srt)

    Returns:
        생성된 SRT 파일의 절대 경로 (str)

    Raises:
        FileNotFoundError: script_path 파일 없음
        ValueError:        시간 태그를 찾을 수 없는 경우
    """
    script_path = pathlib.Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"스크립트 파일 없음: {script_path}")

    if output_path is None:
        output_path = script_path.with_suffix(".srt")
    output_path = pathlib.Path(output_path)

    text     = script_path.read_text(encoding="utf-8")
    sections = _parse_sections(text)

    if not sections:
        raise ValueError(
            f"시간 태그([0~5초 ...] 형식)를 찾을 수 없습니다: {script_path}\n"
            "  → output_script.txt 포맷이 올바른지 확인하세요."
        )

    # SRT 블록 조립
    blocks = []
    for idx, (start_sec, end_sec, content) in enumerate(sections, 1):
        time_line = f"{_to_srt_time(start_sec)} --> {_to_srt_time(end_sec)}"
        blocks.append(f"{idx}\n{time_line}\n{content}")

    srt_text = "\n\n".join(blocks) + "\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(srt_text, encoding="utf-8")

    return str(output_path)


# ─────────────────────────────────────────────────────────────
# CLI: python -m core.srt_generator <script_path> [output_path]
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python -m core.srt_generator <script_path> [output_path]")
        sys.exit(1)

    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) >= 3 else None

    try:
        result = generate_srt(src, dst)
        print(f"[SRT 생성 완료] {result}")
        print()
        print(pathlib.Path(result).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)
