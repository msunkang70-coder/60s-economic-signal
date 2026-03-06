"""
LLM API 진단 스크립트 (Groq)
로컬 터미널에서 실행: python test_gemini.py
"""
import os, sys, re, requests

# ── secrets.toml에서 키 읽기 ─────────────────────────────
secrets_path = ".streamlit/secrets.toml"
groq_key = ""
try:
    content = open(secrets_path, encoding="utf-8").read()
    m = re.search(r'\[groq\].*?api_key\s*=\s*"([^"]+)"', content, re.DOTALL)
    groq_key = m.group(1).strip() if m else ""
except Exception as e:
    print(f"❌ secrets.toml 읽기 실패: {e}")

print("=" * 55)
print("  Groq API 진단 리포트")
print("=" * 55)
print(f"secrets.toml  : {'존재' if os.path.exists(secrets_path) else '없음'}")
print(f"GROQ_API_KEY  : {'환경변수 설정됨' if os.environ.get('GROQ_API_KEY') else '없음'}")
print(f"secrets.toml 키: {'✅ ' + groq_key[:12] + '...' if groq_key and 'YOUR' not in groq_key else '❌ 미설정'}")

effective_key = os.environ.get("GROQ_API_KEY", "").strip() or groq_key
if not effective_key or "YOUR" in effective_key:
    print("\n❌ Groq API 키가 없습니다.")
    print("   1. https://console.groq.com 접속")
    print("   2. API Keys → Create API Key → 복사")
    print("   3. .streamlit/secrets.toml 의 [groq] api_key 에 입력")
    sys.exit(1)

# ── Groq API 직접 호출 ───────────────────────────────────
print(f"\n사용 키: {effective_key[:14]}...")
print("Groq Llama-3.3-70B 호출 중...")

try:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {effective_key}",
            "Content-Type":  "application/json",
        },
        json={
            "model":       "llama-3.3-70b-versatile",
            "messages":    [{"role": "user", "content": "안녕하세요. 한 문장으로 자기소개 해주세요."}],
            "max_tokens":  64,
            "temperature": 0.3,
        },
        timeout=15,
    )
    print(f"HTTP 상태: {resp.status_code}")
    if resp.status_code == 200:
        text = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"✅ Groq 응답 성공: {text[:80]}")
    else:
        err = resp.json().get("error", {})
        print(f"❌ API 오류: {err.get('code', resp.status_code)} — {err.get('message','')[:120]}")
except Exception as e:
    print(f"❌ 연결 오류: {e}")
    sys.exit(1)

# ── summarize_3line 함수 테스트 ──────────────────────────
print("\n" + "-" * 55)
print("summarize_3line 함수 테스트...")
sys.path.insert(0, ".")
try:
    os.environ["GROQ_API_KEY"] = effective_key
    from core.summarizer import summarize_3line
    sample = (
        "정부는 반도체 산업 경쟁력 강화를 위해 10조원 규모의 지원 펀드를 조성한다. "
        "이를 통해 팹리스·소재·장비 기업에 대한 저금리 정책자금을 공급하고, "
        "R&D 세액공제율을 현행 20%에서 30%로 상향한다. "
        "이번 조치로 국내 반도체 생태계가 강화되고 수출 경쟁력이 높아질 것으로 전망된다."
    )
    result, source = summarize_3line(sample, "반도체 지원 펀드 10조원 조성")
    print(f"출처: {source}")
    print(result)
except Exception as e:
    import traceback
    traceback.print_exc()
