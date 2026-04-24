# 조회수 방어의 객체지향 설계

시스템 엔지니어를 위한 교재. Robert C. Martin의 *UML for Java Programmers* 1~4장의 철학을 조회수 유효성 판정 레이어라는 하나의 구체 사례에 적용했습니다.

## 구성

```
book/
├── book.html              # 최종 책 (단일 HTML, SVG 인라인 포함)
├── book.css               # 인쇄 친화적 최소 스타일시트
├── run/
│   ├── init.sh            # 최초 1회: 가상환경 + pytest 설치
│   ├── test.sh            # 여러 번: 계약 테스트 실행
│   └── dev.sh             # 로컬 서버로 책 미리보기
├── src/viewcount/
│   ├── event.py           # ViewEvent 불변 데이터
│   ├── identity.py        # 세션+JA4 기반 지문 추출기
│   ├── firewall.py        # 방어자: 3층 판정
│   └── bot.py             # 공격자: Bot 추상 + Naive/Stealth
├── tests/
│   └── test_contracts.py  # 14개의 TDD 계약 테스트
└── diagrams/
    ├── figN-N.mmd         # mermaid 소스
    └── figN-N.svg         # 렌더링된 SVG (book.html에 인라인됨)
```

## 시작하기

```bash
bash run/init.sh       # 최초 1회
bash run/test.sh       # 테스트 실행
bash run/dev.sh        # localhost:8000/book.html에서 책 열기
```

테스트는 로컬에서 실행하세요 (사용자 기본 설정대로).

## 책 읽기

`book.html`을 브라우저에서 열거나 바로 인쇄하세요. Chrome/Firefox 어느 쪽이든 `Ctrl+P`로 A4 프린터에 바로 출력할 수 있습니다. 다이어그램은 이미 SVG로 인라인되어 있어 JavaScript도 네트워크 연결도 필요 없습니다.

## 계약의 요점

### 방어자 (Firewall)
- `evaluate(event: ViewEvent) -> Decision` 한 개의 공개 메서드
- 3층 판정: 정적 신호(IP, TLS) → 행동 신호(dwell, mouse) → 집계 신호(burst)
- `Verdict.ACCEPT | SUSPICIOUS | REJECT` — 불리언이 아닌 3값 판정
- 모든 판정에 사람이 읽을 수 있는 `reason` 부착 (감사성)

### 공격자 (Bot)
- `Bot` 추상 베이스 클래스 + `NaiveBot`, `StealthBot` 두 구현
- `attack() -> Iterator[ViewEvent]` 한 개의 추상 메서드
- 난이도 차이가 **테스트로 고정**됨: `test_stealth_bot_is_harder_to_detect`

## 다이어그램을 다시 렌더링하려면

```bash
cd diagrams
for f in fig*.mmd; do
  mmdc -i "$f" -o "${f%.mmd}.svg" -c mmd-config.json -p puppeteer-config.json -b white
done
python ../_inline_svgs.py
```
