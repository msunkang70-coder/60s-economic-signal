# 60초 경제신호

KDI 나라경제 월간 이슈를 자동으로 수집·요약해 **산업별 수출 경제 브리핑 대시보드**와 **YouTube Shorts 스크립트**를 생성하는 도구입니다.

---

## 🚀 빠른 시작 (5분 설정)

### 1단계 — 의존성 설치

```bash
git clone https://github.com/<USERNAME>/60sec_econ_signal.git
cd 60sec_econ_signal

pip install -r requirements.txt
```

### 2단계 — 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 편집하여 API 키 입력
```

> 최소 설정: `ECOS_API_KEY`만 있으면 대시보드 기본 기능이 동작합니다.
> API 키 없이도 `data/macro.json`의 수동 값으로 앱이 실행됩니다.

### 3단계 — 앱 실행

```bash
# Streamlit 대시보드 실행
streamlit run app.py

# CLI 스크립트 생성 (선택)
python main.py
```

---

## 📊 주요 기능

### Phase 1 — 기본 수집·요약

| 기능 | 설명 |
|------|------|
| 📰 기사 수집 | KDI EIEC 나라경제 + KOTRA RSS 자동 크롤링 |
| ✍️ 스크립트 생성 | 60초 YouTube Shorts 스크립트 자동 생성 |
| 🎬 SRT 자막 | `output_script.srt` 자동 생성 |
| 📈 거시지표 | 환율·CPI·수출증가율·기준금리 자동 업데이트 (ECOS API) |
| 🌐 Streamlit 대시보드 | 브라우저 기반 경제 브리핑 대시보드 |

### Phase 2 — 산업별 맞춤 분석

| 기능 | 설명 |
|------|------|
| 🏭 산업별 프로필 | 반도체·자동차·배터리·화학·소비재·조선·철강 7개 산업 |
| ⭐ 임팩트 스코어 | 기사별 산업 가중 영향도 (1~5점) 자동 산출 |
| 📊 거시경제 임팩트 | 산업별 거시지표 영향도 (-3.0 ~ +3.0) |
| 🚨 임계값 알림 | 핵심 지표 이상 징후 자동 감지 |
| 📋 액션 브리핑 | 산업별 맞춤 실행 체크리스트 |
| 🔍 전략적 시사점 | 거시지표 기반 전략 질문 자동 생성 |

### Phase 3 — 시나리오·시장 추천·자동화

| 기능 | 설명 |
|------|------|
| 🔮 시나리오 분석 | 환율·유가·금리 변동 시 산업별 영향도 시뮬레이션 |
| 🌏 글로벌 시장 추천 | UN Comtrade 기반 유망 수출 시장 Top 3 추천 |
| 📧 이메일 리포트 | 산업별 브리핑 이메일 자동 발송 (SMTP) |
| 👥 구독자 관리 | 산업별 구독·해지 시스템 |
| ⏰ 월간 자동화 | GitHub Actions — 매월 1일 09:00 KST 8개 산업 병렬 실행 |
| 📊 파이프라인 알림 | Slack/이메일로 실행 결과 요약 알림 |

---

## ⚙️ 환경변수 설정

`.env.example`을 복사하여 `.env` 파일을 만들고 키를 입력하세요.

| 변수 | 용도 | 필수 |
|------|------|:----:|
| `ECOS_API_KEY` | 한국은행 ECOS API (거시지표) | 권장 |
| `GROQ_API_KEY` | Groq LLM 요약 (3줄 요약 고도화) | 선택 |
| `SMTP_HOST` | 이메일 SMTP 호스트 | 이메일 시 |
| `SMTP_PORT` | SMTP 포트 (기본 587) | 이메일 시 |
| `SMTP_USER` | 발신자 이메일 주소 | 이메일 시 |
| `SMTP_PASSWORD` | Gmail 앱 비밀번호 (16자리) | 이메일 시 |
| `ADMIN_EMAIL` | 관리자 알림 수신 주소 | 선택 |
| `DASHBOARD_URL` | Streamlit Cloud 배포 URL | 선택 |
| `SLACK_WEBHOOK_URL` | Slack 파이프라인 알림 | 선택 |
| `CUSTOMS_API_KEY` | 관세청 API | 선택 |
| `COMTRADE_API_KEY` | UN Comtrade API (시장 추천) | 선택 |

### API 키 발급

- **ECOS API**: [ecos.bok.or.kr](https://ecos.bok.or.kr) → 오픈 API → 인증키 신청 (무료)
- **Groq API**: [console.groq.com](https://console.groq.com) → API Keys (무료 티어)
- **Gmail 앱 비밀번호**: Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호 생성

### Streamlit Cloud 배포 시

Streamlit Cloud에서는 `.env` 대신 **Secrets** 설정을 사용합니다.
앱 대시보드 → **⋮ Settings > Secrets** 탭에 TOML 형식으로 입력:

```toml
[ecos]
api_key = "YOUR_ECOS_API_KEY"

[email]
sender = "your_email@gmail.com"
password = "your_app_password"
recipients = "user1@example.com, user2@example.com"
```

---

## 🤖 GitHub Actions 자동화 설정

### 워크플로 구조

| 워크플로 | 파일 | 설명 |
|----------|------|------|
| 월간 경제신호 | `monthly_run.yml` | 스크립트 생성 + 이메일 발송 (단일) |
| 산업별 병렬 실행 | `monthly_signal.yml` | 8개 산업 matrix 병렬 실행 + Slack 알림 |

### GitHub Secrets 설정

GitHub 리포지터리 → **Settings > Secrets and variables > Actions > New repository secret**

| Secret 이름 | 값 | 필수 |
|-------------|------|:----:|
| `ECOS_API_KEY` | 한국은행 ECOS API 키 | 권장 |
| `EMAIL_SENDER` | 발신자 Gmail 주소 | 이메일 시 |
| `EMAIL_PASSWORD` | Gmail 앱 비밀번호 (16자리) | 이메일 시 |
| `EMAIL_RECIPIENTS` | 수신자 주소 (쉼표 구분) | 이메일 시 |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL | 선택 |
| `ADMIN_EMAIL` | 관리자 알림 수신 주소 | 선택 |

> 이메일 Secret 3개가 모두 없으면 발송을 건너뜁니다 — 다른 기능에 영향 없음

### 수동 실행

GitHub → Actions 탭 → **Monthly Economic Signal** → **Run workflow**

- `industry`: 특정 산업(예: `반도체`) 또는 `all` (8개 전체 병렬)

---

## 🏭 산업별 실행 방법

### 대시보드에서 산업 선택

사이드바 **🏭 산업 선택** 드롭다운에서 원하는 산업을 선택하면 해당 산업에 맞춤화된 브리핑이 표시됩니다.

| 산업 | 아이콘 | 핵심 경제 변수 |
|------|:------:|--------------|
| 반도체·디스플레이 | 🔬 | 미국 반도체 규제, AI 반도체 수요, 환율 |
| 자동차·부품 | 🚗 | 환율, 철강 가격, 미국 관세 정책 |
| 석유화학·정밀화학 | 🧪 | 국제유가, 원자재 가격, CBAM |
| 소비재·식품 | 🛒 | 글로벌 소비 경기, 물류비, 환율 |
| 2차전지·배터리 | 🔋 | 리튬 가격, 미국 IRA, 전기차 판매 |
| 조선·해양 | 🚢 | 선박 수주, 해운 운임, 철강 가격 |
| 철강·금속 | 🏗️ | 철광석 가격, 중국 철강 수출, CBAM |

### CLI 스크립트 생성 (산업별)

```bash
# 환경변수로 산업 지정
INDUSTRY=반도체 python main.py
INDUSTRY=자동차 python main.py

# 기본값 (일반)
python main.py
```

---

## 프로젝트 구조

```
60sec_econ_signal/
├── app.py                        # Streamlit 대시보드
├── main.py                       # CLI 스크립트 생성기
├── requirements.txt
├── .env.example                  # 환경변수 템플릿
├── .streamlit/
│   ├── config.toml               # Streamlit 테마·서버 설정
│   └── secrets.toml.example      # Secrets 설정 가이드
├── core/
│   ├── fetcher.py                # KDI 기사 수집
│   ├── extra_sources.py          # KOTRA RSS 등 멀티소스
│   ├── summarizer.py             # 텍스트 요약 (규칙 + Groq LLM)
│   ├── srt_generator.py          # SRT 자막 변환
│   ├── ecos.py                   # ECOS API 연동 (거시지표)
│   ├── industry_config.py        # 산업별 프로필 설정
│   ├── impact_scorer.py          # 임팩트 스코어 엔진
│   ├── signal_interpreter.py     # 거시지표 신호 해석
│   ├── macro_signal_engine.py    # 매크로 신호 감지
│   ├── scenario_engine.py        # 시나리오 분석 엔진
│   ├── market_recommender.py     # 글로벌 시장 추천 엔진
│   ├── strategy_generator.py     # 전략 질문 자동 생성
│   ├── action_checklist.py       # 액션 체크리스트
│   ├── emailer.py                # 이메일 자동 발송 (SMTP)
│   ├── pipeline_notifier.py      # 파이프라인 알림 (Slack/이메일)
│   ├── subscription.py           # 구독자 관리
│   ├── analytics.py              # 사용자 이벤트 로깅
│   ├── content_manager.py        # 콘텐츠 이력 관리
│   ├── feedback_store.py         # 피드백 저장
│   ├── watchlist.py              # 관심 지표 워치리스트
│   ├── storage.py                # 로컬 캐시 (history.db)
│   └── utils.py                  # 유틸리티 함수
├── data/
│   ├── macro.json                # 거시지표 (ECOS API 또는 수동)
│   ├── content_db.json           # 콘텐츠 생성 이력
│   ├── score_history.json        # 임팩트 스코어 히스토리
│   ├── subscribers.json          # 구독자 목록
│   └── feedback.json             # 사용자 피드백
├── outputs/
│   ├── output_script.txt         # 최신 스크립트
│   ├── output_script.srt         # 최신 SRT 자막
│   └── latest_output_script.txt  # GitHub Actions 자동 갱신
└── .github/workflows/
    ├── monthly_run.yml           # 월간 자동화 (단일 실행)
    └── monthly_signal.yml        # 산업별 병렬 자동화 (matrix)
```

---

## 로컬 개발 — Secrets 설정

`.streamlit/secrets.toml.example`을 복사해서 실제 키를 입력하세요.

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# secrets.toml 편집 후 api_key 입력
```

`secrets.toml`은 `.gitignore`에 포함돼 있어 GitHub에 올라가지 않습니다.
