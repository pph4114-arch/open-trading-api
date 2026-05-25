# 밸류체인 섹터 순환 (Sector Rotation)

심플 회전 파이프라인: 밸류체인 종목 수집 → 외인·기관 수급 누적 → 섹터별 강도 산정 → Top-N 종목 진입 → 사이클 청산(SL/TP/수급 약화) → pandas 백테스트(체결·비용·사이클 분해) → 결과 산출(거래원장·에쿼티·사이클·요약)

기반 데이터는 KIS Open API (일봉 시세 + 종목별 투자자 매매동향).
한 번에 1개 섹터의 Top-N 종목만 보유하며, 청산되면 다음 사이클에서 그 시점 가장 강한 섹터로 회전.

## 구조

```
backtester/sector_rotation/
├── __init__.py
├── README.md
├── value_chain.yaml        # 4섹터 × 티어 × 종목 매핑 (사용자 큐레이션)
├── data_fetcher.py         # KIS API 호출 + parquet 캐시
├── engine.py               # 백테스트 엔진 (pandas, look-ahead-safe)
├── metrics.py              # 성과 지표 (수익률/Sharpe/MDD/사이클 통계)
├── run_backtest.py         # CLI 엔트리포인트
└── cache/                  # 종목별 가격/수급 parquet (자동 생성, gitignored)
    ├── prices/  005930.parquet  ...
    └── flows/   005930.parquet  ...

backtester/examples/output/sector_rotation/   ← 실행 결과 (실행할 때마다 갱신)
├── equity_curve.csv
├── trades.csv
├── cycles.csv
└── summary.json
```

## 섹터 (`value_chain.yaml`)

| 섹터 | 티어 | 종목 |
|------|------|------|
| 반도체 | tier1_leading | 삼성전자(005930), SK하이닉스(000660) |
| 반도체 | tier2_equipment_front | 원익IPS(240810), 주성엔지니어링(036930), 피에스케이(319660) |
| 반도체 | tier3_equipment_back | 한미반도체(042700), 이오테크닉스(039030) |
| 반도체 | tier4_materials | SK머티리얼즈(036490), 코스모신소재(005070) |
| 반도체 | tier5_pcb | 심텍(222800), 대덕전자(008060) |
| 2차전지 | tier1_leading | LG에너지솔루션(373220), 삼성SDI(006400), SK이노베이션(096770) |
| 2차전지 | tier2_cathode | 에코프로비엠(247540), 포스코퓨처엠(003670), 엘앤에프(066970) |
| 2차전지 | tier3_anode_separator | 대주전자재료(078600), SK아이이테크놀로지(361610) |
| 2차전지 | tier4_equipment | 피엔티(137400) |
| 로봇 | tier1_leading | 두산로보틱스(454910), 레인보우로보틱스(277810) |
| 로봇 | tier2_reducer_motor | 에스피지(058610), 로보티즈(108490) |
| 로봇 | tier3_components | 큐에스아이(066310) |
| 전력 | tier1_leading | 효성중공업(298040), HD현대일렉트릭(267260), LS ELECTRIC(010120) |
| 전력 | tier2_transformer | 산일전기(062040), 제룡전기(192410) |
| 전력 | tier3_nuclear | 두산에너빌리티(034020), 한전기술(052690), 한전KPS(051600) |
| 전력 | tier4_cable | 대한전선(001440) |

티어는 분석/리포팅용 메타데이터. 엔진은 섹터 단위로만 보유하며, 섹터 내 Top-N 은 5일 수급 강도로 동적 결정한다.
종목 추가/수정은 `value_chain.yaml` 직접 편집.

## 실행

```bash
# KIS Open API 인증 사전 설정 (~/KIS/config/kis_devlp.yaml)
# 의존성 설치 (저장소 루트의 uv 환경 사용 권장)
uv sync   # backtester/ 디렉토리에서

# 기본 실행 (2년 백테스트)
cd backtester
python sector_rotation/run_backtest.py --start 2023-01-01 --end 2024-12-31

# 파라미터 조정
python sector_rotation/run_backtest.py --top-n 3 --take-profit 0.30 --stop-loss -0.07
python sector_rotation/run_backtest.py --flow-window 10 --sector-strength-min 1e9
python sector_rotation/run_backtest.py --start 2020-01-01 --end 2024-12-31     # 장기

# 그 외 옵션
python sector_rotation/run_backtest.py --transaction-cost 0.003 --force-refresh
```

## 파라미터

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--start` / `--end` | 2023-01-01 / 2024-12-31 | 백테스트 기간 |
| `--flow-window` | 5 | 수급 누적 윈도우 (일) |
| `--top-n` | 5 | 섹터 내 보유 종목 수 |
| `--take-profit` | 0.40 | 익절 임계 (사이클 수익률) |
| `--stop-loss` | -0.10 | 손절 임계 |
| `--sector-strength-min` | 0.0 | 보유 섹터 강도 임계 (이하면 회전) |
| `--entry-strength-min` | 0.0 | 신규 진입 시 최소 섹터 강도 |
| `--top-n` | 5 | 섹터 내 균등가중 종목 수 |
| `--transaction-cost` | 0.0025 | 편도 거래비용 (수수료+세금 근사) |
| `--initial-capital` | 100_000_000 | 초기자본 (원) |
| `--force-refresh` | off | 캐시 무시하고 KIS API 재호출 |

## 신호 (3-layer)

신호는 세 층의 확증을 거침:

**1) 섹터 강도 (sector_strength)**

```
sector_strength(sector, t) = Σ_{s ∈ sector} [ flow_window(s, t) ]
flow_window(s, t)          = Σ_{i=0..N-1} ( foreign_net(s, t-i) + institution_net(s, t-i) )
```

- `foreign_net`, `institution_net` 은 KIS `investor_trade_by_stock_daily` 의 거래대금 기준 (`frgn_ntby_tr_pbmn`, `orgn_ntby_tr_pbmn`)
- `N = --flow-window` (기본 5일)
- 미보유 시: `sector_strength` 가 가장 크고 `--entry-strength-min` 이상인 섹터를 선택

**2) 종목 선정 (Top-N)**

```
candidates(sector, t) = { s ∈ sector | flow_window(s, t) > 0 }
picks(t)              = top-N by flow_window  among candidates(sector*, t)
```

- 5일 누적 순매수가 양(+)인 종목만 후보
- 그 중 강한 순서로 N개 → 균등 가중 매수
- 후보 < N 이면 가능한 수만 매수

**3) 청산 (우선순위, 어느 하나라도 발동 시 회전)**

| 우선순위 | 조건 | 사유 코드 |
|----------|------|-----------|
| 1 | `cycle_return ≤ stop_loss_pct` | `stop_loss` |
| 2 | `cycle_return ≥ take_profit_pct` | `take_profit` |
| 3 | `sector_strength(held, t) ≤ sector_strength_min` | `sector_weak` |
| - | 백테스트 마지막 날 | `end_of_backtest` |

```
cycle_return(t) = position_value(t) / position_value_at_entry - 1
```

→ 청산은 다음 영업일 시가에 일괄 체결, 같은 날 새 섹터로 진입하지 않고 **그 다음 영업일 시가에 신규 진입**.

## 체결 규칙

- **신호 시점**: 매 영업일 종가
- **체결 시점**: 익일 시가 (`open_df[t+1]`) — look-ahead 방지
- **수량 산정**: 균등 가중. `budget_per_stock = (cash × (1 - cost)) / N`, `shares = floor(budget / price)`
- **비용**: 매수·매도 각 `--transaction-cost` (편도) 차감
- **시가 결손**: 시가가 NaN 인 경우 청산 시점은 종가로 대체 (드물게 발생)

## 백테스트 산출물

```
backtester/examples/output/sector_rotation/
├── equity_curve.csv    # 일별 평가액·현금·포지션가치·보유섹터·사이클수익률
├── trades.csv          # 매매 기록 (날짜·매수/매도·종목·가격·수량·비용·사유)
├── cycles.csv          # 사이클별 진입/청산/수익률/사유/보유종목
└── summary.json        # 성과 지표 + 설정 스냅샷
```

### 콘솔 출력 예시

```
──────────────────────────────────────────────────
백테스트 성과 요약
──────────────────────────────────────────────────
  초기자본         :     100,000,000 원
  최종평가         :     145,200,000 원
  총수익률         :         45.20%
  CAGR             :         20.45%
  Sharpe           :           1.250
  Sortino          :           1.820
  MDD              :        -12.34%
──────────────────────────────────────────────────
  사이클 수        :              12
  승률             :         58.33%
  평균 사이클 수익 :          4.20%
  평균 승          :         11.80%
  평균 패          :         -5.40%
  Profit Factor    :           2.180
──────────────────────────────────────────────────
  청산 사유 분포   :
    take_profit        :     2 회
    stop_loss          :     3 회
    sector_weak        :     7 회
──────────────────────────────────────────────────
```

### summary.json 필드

| 필드 | 설명 |
|------|------|
| `initial_capital`, `final_equity` | 시작/종료 평가액 |
| `total_return`, `cagr` | 누적·연환산 수익률 |
| `sharpe`, `sortino` | 위험조정 수익 (연환산, 무위험수익률=0) |
| `max_drawdown` | 최대 낙폭 (peak-to-trough) |
| `n_cycles`, `win_rate` | 사이클 수 / 승률 |
| `avg_cycle_return`, `avg_win`, `avg_loss` | 사이클 평균 수익 / 평균 승 / 평균 패 |
| `profit_factor` | 총승 / |총패| |
| `exit_reasons` | 청산 사유별 빈도 |
| `config` | 실행 시점 파라미터 스냅샷 |

## 수집 동작

1. `value_chain.yaml` 의 모든 종목을 순회
2. 각 종목에 대해:
   - `cache/prices/<code>.parquet` — 캐시 존재 + 기간 충족 시 즉시 로드
   - 없거나 부족하면 `inquire_daily_itemchartprice` 호출 (100건/회 페이지네이션)
3. `cache/flows/<code>.parquet` — 동일 패턴, `investor_trade_by_stock_daily` (래퍼가 tr_cont 자동 페이지네이션)
4. API 호출 사이 `throttle_sec=0.25` 대기 (KIS rate limit 보호)
5. 모든 응답을 parquet 으로 영구 저장

→ 최초 수집 ~1~2분 (34 종목 × 가격+수급 2종 × 2년), 이후 동일 기간 재실행은 캐시만 읽고 수 초 내 완료.
`--force-refresh` 로 강제 재수집.

## 한계 및 주의

- **수급 데이터는 장 마감 후 집계**. "전일 신호 → 익일 시가 체결"로 look-ahead 는 차단했으나, 실거래 시 익일 시가 슬리피지/유동성 별도 고려 필요.
- **분봉/장중 신호 미지원**: KIS 분봉 historical 데이터가 당일만 제공되어 일봉 신호로 한정.
- **수정주가**: `fid_org_adj_prc="0"` (수정주가) 로 받지만 무상증자/액면분할 직후 수일은 노이즈 가능.
- **체결가 단순화**: 시가 일괄 체결, 호가단위/유동성/슬리피지 미반영.
- **상장폐지/거래정지**: 백테스트 기간 내 발생 시 NaN 처리. 향후 mask 필요.

## 향후 확장 (미구현)

다음 항목은 현재 미구현. 1차 검증 결과를 보고 필요 시 추가:

- **벤치마크 비교** — KOSPI/KODEX 200 대비 알파/베타, 상관관계
- **시각화** — equity curve, 사이클별 수익률 분포, 섹터 점유율 타임라인 (matplotlib/plotly)
- **검증 (Validation)**:
  - **Walk-forward**: in-sample / out-of-sample 분할 백테스트
  - **Monte Carlo permutation**: 포지션 시퀀스 셔플 → 타이밍 신호 유의성 검정
- **파라미터 grid search** — `--take-profit`, `--stop-loss`, `--flow-window`, `--top-n` 동시 탐색
- **포지션 사이즈 다양화** — Top-N 균등 → 수급 강도 가중
- **모의투자 forward test** — `strategy_builder/` order executor 연결
