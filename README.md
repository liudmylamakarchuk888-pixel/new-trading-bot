# Polymarket BTC/ETH Value Bot (Phase 1~3)

BTC/ETH 가격 기반 Polymarket 시장만 대상으로 하는 Value Betting 봇입니다.
**실주문은 넣지 않습니다.** 데이터 수집 → 백테스트 → 페이퍼 트레이딩까지만 구현되어 있습니다.

핵심 원칙: `edge = fair_probability - market_ask - cost >= min_edge` 일 때만 (가상) 진입.

## 설치

```bash
pip install -r requirements.txt
```

인증/지갑/API 키가 필요 없습니다. Polymarket Gamma API, CLOB 공개 엔드포인트,
마켓 WebSocket, Binance 공개 피드만 사용합니다.

## 데이터베이스 (PostgreSQL)

저장소는 PostgreSQL을 사용하며 `BOT_DATABASE_URL`(.env)로 선택합니다.
`trading_bot` 데이터베이스와 스키마는 첫 실행 시 자동 생성됩니다.

```bash
# 로컬 개발용 PostgreSQL 시작
docker compose up -d
```

`.env` (예시는 `.env.example` 참고):

```
# 로컬 개발
BOT_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/trading_bot

# 로컬에서 VPS(Supabase Postgres, 5432 -> 54322 포트 매핑)의 DB 사용
# BOT_DATABASE_URL=postgresql://postgres:postgres-dev-password@141.136.44.1:54322/trading_bot?sslmode=disable

# VPS 위에서 봇을 직접 실행할 때
# BOT_DATABASE_URL=postgresql://postgres:postgres-dev-password@localhost:54322/trading_bot?sslmode=disable
```

## 사용 순서

```bash
# 1. 데이터 수집 데몬 (수 시간~수 일 가동해 데이터 축적)
python -m app.main collect

# 2. 수집된 데이터로 백테스트
python -m app.main backtest
python -m app.main backtest --from 2026-06-09T00:00:00 --to 2026-06-10T00:00:00

# 3. 페이퍼 트레이딩 (수집 + 가상 주문/체결/정산, 실주문 없음)
python -m app.main paper

# 4. 리포트 (PnL, 승률, 엣지 분포, 차익 기회)
python -m app.main report
```

## 긴급 정지 (Kill Switch)

프로젝트 루트에 `KILL_SWITCH` 파일을 만들면 봇이 신규 진입을 멈추고
열려 있는 가상 주문을 전부 취소합니다.

```bash
# PowerShell
New-Item KILL_SWITCH
```

## 설정

`.env` 파일 또는 환경변수(`BOT_` 접두사)로 설정을 바꿀 수 있습니다.
기본값은 `app/config.py` 참고.

```
BOT_BANKROLL=500
BOT_MIN_EDGE=0.02
BOT_COST=0.01
BOT_MAX_SPREAD=0.08
BOT_DAILY_LOSS_FRAC=0.03
```

## 리스크 규칙 (페이퍼 모드에도 동일 적용)

- 단일 거래 ≤ 자금의 1%
- 시장 하나당 노출 ≤ 자금의 3%
- 일일 손실 한도 3%, 주간 8%
- 3연속 손실 시 자동 정지
- 만료 5분 전 신규 진입 금지
- 스프레드 8% 초과 시 진입 금지
- maker 주문만 시뮬레이션 (taker/시장가 없음)
- 같은 토큰에 포지션이 있으면 추가 진입 금지 (물타기 금지)

## 구조

```
app/
  main.py            # CLI: collect | paper | backtest | report
  config.py
  data/              # Gamma 시장 발견, CLOB REST/WS, Binance 피드, 레코더
  strategy/          # 확률 모델(N(d2)+EWMA 변동성), 엣지 판정, 차익 스캐너
  paper/             # 가상 maker 체결 엔진, 포지션/정산 관리
  backtest/          # 수집 데이터 리플레이 백테스터, 성과 지표
  risk/              # 리스크 엔진, kill switch
  storage/           # PostgreSQL (psycopg), 스키마, 데이터 모델
```

## Phase 4 (실거래) 진입 조건

백테스트와 페이퍼 트레이딩 **모두**에서 비용 차감 후 양(+)의 기대값이
확인되기 전에는 실주문 코드를 작성하지 않습니다.
