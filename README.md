# 60초 경제신호

KDI 나라경제 월간 이슈를 자동으로 수집·요약해 **YouTube Shorts 스크립트**와 **SRT 자막**을 생성하는 도구입니다.

---

## 기능

| 기능 | 설명 |
|------|------|
| 📰 기사 수집 | KDI EIEC 나라경제 월간 이슈 자동 크롤링 |
| ✍️ 스크립트 생성 | 60초 YouTube Shorts 스크립트 자동 생성 |
| 🎬 SRT 자막 | `output_script.srt` 자동 생성 (시간 태그 → SRT 변환) |
| 📈 거시지표 | 환율·CPI·수출증가율 자동 업데이트 (ECOS API) |
| 🌐 Streamlit 앱 | 브라우저 기반 대시보드 (문서 탐색·다운로드) |
| ⏰ 월간 자동화 | GitHub Actions — 매월 2일 09:00 KST 자동 실행 |

---

## 빠른 시작 (로컬)

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 스크립트 생성 (outputs/output_script.txt + .srt 생성)
python main.py

# 3. Streamlit 앱 실행
streamlit run app.py
```

---

## Streamlit Cloud 배포

### 1단계 — GitHub 리포지터리 생성 및 푸시

```bash
git init
git add .
git commit -m "init: 60초 경제신호 초기 커밋"
git remote add origin https://github.com/<USERNAME>/<REPO>.git
git push -u origin main
```

### 2단계 — Streamlit Cloud 연결

1. [share.streamlit.io](https://share.streamlit.io) 접속 후 GitHub 로그인
2. **New app** 클릭
3. 리포지터리·브랜치·`app.py` 선택 후 **Deploy**

### 3단계 — Secrets 설정 (ECOS API 선택)

Streamlit Cloud 앱 대시보드 → **⋮ Settings > Secrets** 탭에 아래 내용 붙여넣기:

```toml
[ecos]
api_key = "YOUR_ECOS_API_KEY_HERE"
```

> ECOS API 키 무료 발급: [ecos.bok.or.kr](https://ecos.bok.or.kr) → 오픈 API → 인증키 신청
> 키가 없으면 `data/macro.json`의 수동 값이 그대로 표시됩니다.

### 4단계 — GitHub Actions Secret 설정 (월간 자동화)

GitHub 리포지터리 → **Settings > Secrets and variables > Actions > New repository secret**

| 이름 | 값 | 필수 |
|------|----|----|
| `ECOS_API_KEY` | 한국은행 ECOS API 키 | 선택 |
| `EMAIL_SENDER` | 발신자 Gmail 주소 | 이메일 발송 시 |
| `EMAIL_PASSWORD` | Gmail 앱 비밀번호 (16자리) | 이메일 발송 시 |
| `EMAIL_RECIPIENTS` | 수신자 주소 (쉼표 구분) | 이메일 발송 시 |

> **Gmail 앱 비밀번호 발급**: Google 계정 → 보안 → 2단계 인증 활성화 → 앱 비밀번호 → 생성
> 3개 이메일 Secret이 모두 없으면 발송을 건너뜀 — 다른 기능에 영향 없음

---

## 프로젝트 구조

```
60sec_econ_signal/
├── app.py                    # Streamlit 대시보드
├── main.py                   # CLI 스크립트 생성기
├── requirements.txt
├── .streamlit/
│   ├── config.toml           # Streamlit 테마·서버 설정
│   └── secrets.toml.example  # Secrets 설정 가이드
├── core/
│   ├── ecos.py               # ECOS API 연동 (거시지표)
│   ├── fetcher.py            # KDI 기사 수집
│   ├── summarizer.py         # 텍스트 요약
│   ├── srt_generator.py      # SRT 자막 변환
│   ├── content_manager.py    # 콘텐츠 이력 (content_db.json)
│   ├── emailer.py            # 이메일 자동 발송 (SMTP)
│   └── storage.py            # 로컬 캐시 (history.db)
├── data/
│   ├── macro.json            # 거시지표 (ECOS API 또는 수동)
│   └── content_db.json       # 콘텐츠 생성 이력
├── outputs/
│   ├── output_script.txt     # 최신 스크립트 (GitHub Actions 자동 갱신)
│   ├── output_script.srt     # 최신 SRT 자막
│   └── latest_output_script.txt
└── .github/workflows/
    └── monthly_run.yml       # 월간 자동화 워크플로
```

---

## 로컬 개발 — Secrets 설정

`.streamlit/secrets.toml.example`을 복사해서 실제 키를 입력하세요.

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# secrets.toml 편집 후 api_key 입력
```

`secrets.toml`은 `.gitignore`에 포함돼 있어 GitHub에 올라가지 않습니다.
