# ranker

Naver 통합검색 블로그 포스트 순위 추적기. CLI 도구 + FastAPI 서비스 + React SPA.

## 구성

| 컴포넌트 | 위치 | 설명 |
|----------|------|------|
| CLI 라이브러리 | `src/ranker/` | Playwright 기반 검색·매칭·dwell 엔진. DSL 매니페스트 입력. |
| HTTP API 서버 | `src/ranker_service/` | FastAPI. 매니페스트 업로드 → subprocess로 `ranker` 실행 → 결과 반환. |
| 웹 SPA | `webapp/` | React 19 + Vite 8. 업로드 폼 + 작업 목록 + 결과 다운로드. |
| API 명세 | `docs/openapi.yaml` | OpenAPI 3.2.0. |

## 빠른 시작

### 단순 CLI 사용

```bash
# 매니페스트 작성: examples/dev.yaml 참고
.venv/bin/ranker examples/dev.yaml
# → examples/ranks.yaml 에 결과
```

### Docker Compose (전체 스택)

```bash
# 개발 — hot reload (포어그라운드, 로그 출력)
scripts/dev-up.sh        # → webapp http://localhost:5173 / api http://localhost:8000
scripts/dev-down.sh      # 컨테이너 정리, 볼륨 보존
scripts/dev-down.sh -v   # 볼륨 포함 전부 삭제

# 운영 — nginx + 단일 포트, 디태치드
scripts/prod-up.sh       # → http://localhost
scripts/prod-down.sh
```

스크립트는 모두 추가 docker compose 인자를 통과시킵니다 (예: `scripts/dev-up.sh -d` 디태치).

프록시 사용 시 `.env` 작성:
```bash
cp .env.example .env
# PROXYEMPIRE_USERNAME, PROXYEMPIRE_PASSWORD 채움
```

### 직접 실행 (Docker 없이)

```bash
# 의존성 + 서버 extra 설치
uv pip install --python .venv/bin/python -e '.[server]'

# API 서버
.venv/bin/python -m uvicorn ranker_service.api:app --reload

# (별도 터미널) 웹앱
cd webapp && npm install && npm run dev
```

## 매니페스트 DSL

`examples/` 참고:
- [examples/posts.yaml](examples/posts.yaml) — 추적할 포스트 목록 (보고서)
- [examples/dev.yaml](examples/dev.yaml) — 로컬 검증 (프록시 X, 1회 실행)
- [examples/prod.yaml](examples/prod.yaml) — 운영 (4 Job 병렬 + 프록시 + post_visit)

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `RANKER_HEADFUL` | `0` | `1` → Chromium 창 띄우기 (디버깅용) |
| `RANKER_DEBUG` | `0` | `1` → debug/ 디렉토리에 raw HTML 덤프 |
| `RANKER_SERVICE_JOBS_DIR` | `./jobs` | API 서버의 작업 저장 루트 |
| `RANKER_SERVICE_MAX_CONCURRENT_JOBS` | `4` | 동시 실행 가능한 ranker subprocess 수 |
| `RANKER_BIN` | (auto) | API 서버가 호출할 ranker 실행 파일 경로 |
| `PROXYEMPIRE_USERNAME` | — | ProxyEmpire 계정 stem (proxy 사용 시 필수) |
| `PROXYEMPIRE_PASSWORD` | — | ProxyEmpire 패스워드 (proxy 사용 시 필수) |

## 테스트

```bash
.venv/bin/python -m pytest
```
