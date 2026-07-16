# 📊 나투비 브리핑 — 정적 웹 버전

실적 데이터(DART) 분석부터 시장 심리 파악, 모의투자까지 지원하는 투자 파트너.
**GitHub Actions**가 매 거래일 장 마감 후 데이터를 자동 수집·분석하고, **GitHub Pages**가 결과를 웹으로 서빙합니다. 별도 서버가 필요 없습니다.

```
┌─ 평일 17:10 KST (GitHub Actions) ─────────────┐
│  build_data.py                                │
│  · KRX 전 종목 시세 수집 (FinanceDataReader)  │
│  · 공포-탐욕 지수 계산                        │
│  · DART 실적 + 기술적 지표 2단계 스크리닝     │
│  → data/*.json 커밋                           │
└───────────────────────┬───────────────────────┘
                        ▼
┌─ GitHub Pages ────────────────────────────────┐
│  index.html — 브라우저가 data/*.json 을 읽어  │
│  브리핑·추천·포트폴리오·모의투자 화면 렌더링  │
└───────────────────────────────────────────────┘
```

## 🚀 배포 방법 (5분)

### 1. 저장소 만들기
GitHub에서 새 repo 생성 후 이 폴더 전체를 push:

```bash
git init
git add .
git commit -m "나투비 웹 초기 버전"
git branch -M main
git remote add origin https://github.com/<아이디>/<repo이름>.git
git push -u origin main
```

### 2. DART API 키 등록 (Secrets)
- repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DART_API_KEY` / Value: 본인의 DART 오픈API 키
- ⚠️ 키를 코드에 직접 넣지 마세요. 기존에 코드에 포함했던 키는 [DART](https://opendart.fss.or.kr)에서 재발급을 권장합니다.
- 키가 없어도 동작합니다 — 이 경우 실적 분석은 건너뛰고 기술적 지표만으로 스크리닝합니다.

### 3. GitHub Pages 켜기
- repo → **Settings → Pages**
- Source: `Deploy from a branch` / Branch: `main` / 폴더: `/ (root)` → Save
- 잠시 후 `https://<아이디>.github.io/<repo이름>/` 으로 접속 가능

### 4. 첫 데이터 생성
- repo → **Actions → "데이터 자동 갱신" → Run workflow** (수동 실행)
- 완료되면 `data/` 폴더에 JSON이 커밋되고, 이후 평일 17:10(KST)마다 자동 갱신됩니다.
- 갱신 주기를 바꾸려면 `.github/workflows/update-data.yml`의 `cron`을 수정하세요.

## 💻 로컬에서 미리보기

```bash
python build_data.py --sample   # 외부 통신 없이 가짜 샘플 데이터 생성
python -m http.server           # http://localhost:8000 접속
```

실제 데이터로 테스트하려면:

```bash
pip install -r requirements.txt
DART_API_KEY=본인키 TOP_N=30 python build_data.py
```

> `index.html`을 더블클릭(file://)으로 열면 브라우저 보안 정책 때문에 JSON을 읽지 못합니다. 반드시 `http.server` 또는 GitHub Pages로 여세요.

## 📁 구조

| 경로 | 역할 |
|---|---|
| `index.html` | 웹앱 전체 (단일 파일 · Plotly CDN 사용) |
| `build_data.py` | 데이터 수집·분석 파이프라인 |
| `.github/workflows/update-data.yml` | 평일 장 마감 후 자동 실행 스케줄 |
| `data/market.json` | 공포-탐욕 점수 · KOSPI · 시장 폭 |
| `data/prices.json` | 전 종목 종가/등락률 (포트폴리오·모의투자용) |
| `data/sectors.json` | 시총 상위 500 섹터 트리맵 데이터 |
| `data/screening.json` | 2단계 스크리닝 결과 + 종목별 차트 시계열 |

## ⚙️ 조정 포인트

- **스크리닝 대상 수**: 워크플로우의 `TOP_N` 환경변수 (기본 100 · 늘릴수록 실행 시간 증가)
- **포트폴리오/모의투자 데이터**: 브라우저 localStorage에 저장 — 기기·브라우저별로 독립적이며 서버에 전송되지 않습니다
- **Streamlit 원본 대비 제외된 것**: 금융 퀴즈(요청으로 제외), AI 심층 분석(Ollama 로컬 의존), 나스닥 스크리닝(추후 확장 가능)

## ⚠️ 유의사항

- 데이터는 **일 단위 확정치**입니다 (장중 실시간 아님)
- 본 서비스의 지표는 참고용이며 최종 투자 판단과 책임은 투자자 본인에게 있습니다
