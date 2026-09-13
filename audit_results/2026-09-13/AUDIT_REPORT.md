# Indian automated trading application: audit and paper evidence

Audit date: 13 September 2026. Repository: Automated-trading-ml-project. Running deployment: `trading-api`, host port 8100. **Verdict: profitability is unproven, the running paper account is losing money, and confirmed safety defects prevent a recommendation for unattended real-money operation.**

The application has useful engineering: an explicit execution gate, an order scheduler, persisted exits, reconciliation, a portfolio guardian, and extensive regression tests. Those features do not establish trading edge. The most serious audit finding is a reproducible order-classification bypass that can defeat the kill switch in PAPER execution.

## What the running application actually reports

These are authenticated-header, read-only GET observations of the running container, not results from a newly created demo account. Its database summary identifies PostgreSQL as the active backend. SHA-256 checks matched seven critical deployed Python files to this checkout: runtime, risk engine, simulated broker, backtest engine, metrics, market hours, and portfolio ledger.

| Observation | Result |
|---|---:|
| Execution mode | PAPER; live not armed; live trading flag off |
| Starting capital | ₹1,000,000.00 |
| Current account equity | ₹965,949.14 |
| Equity change from starting capital | **₹-34,050.86 (-3.41%)** |
| Open positions | 0 |
| Recorded production paper fills | 312; no test-tagged rows in this API response |
| Recorded fill period | 10 August–11 September 2026 |
| Strategies in those fills | 156 short_vol_condor, 144 short_vol_exit, 12 exit_manager:expiry |
| Recorded charges across those fills | ₹7,746.68 |
| Current process activity | Agent running, market CLOSED, zero scans since this process restarted |
| Neural return predictor | Moving-average heuristic; no validated return model loaded |
| LLM council | Local inference available; advisory; not evidence of profitable predictions |

The account's `realized_pnl` field reports about −₹31,697.51, while the account-equity change is −₹34,050.86. These are different accounting measures. The entire fill history, historical lot sizes, fees, and restart baselines need reconciliation before treating either series as independently audited brokerage accounting. I use the account-equity change above because the account is currently flat. This is an application-reported paper loss, not a verified real-money brokerage loss.

The target tracker hardcodes **₹5 crore annual profit**, versus ₹10 lakh starting capital (`portfolio/target.py:21`). That requires annual profit equal to 50 times starting capital (5,000%). It is a target input, not a forecast. None of the observed evidence supports it.

The local `data/trading.db` is a different, stale SQLite dataset: 480 fills, including 73 explicitly tagged tests; the other 407 are `momentum_v1` and target-exit records. `risk/compliance.py` itself labels `momentum_v1` a stub/test strategy. Repeated nearly identical starting-balance snapshots mean this local file must not be used as the deployment's performance record.

## Work actually performed

- Inventoried 388 Python/TypeScript source and test files, approximately 99,244 lines, plus 208 cached historical CSVs. Parsed Python source and checked the CSV timestamp inventories; no duplicate timestamps were found in those inventories.
- Traced the critical routes through agent decisions, strategy selection, risk checks, scheduler, simulated/live broker adapters, fills, ledger, exits, restoration, metrics, and dashboard contracts. Reviewed deployment/auth configuration, model activation and promotion evidence.
- Ran the complete existing backend pytest suite in Python 3.12, with isolated SQLite defaults and no local broker credentials loaded: **1,386 passed, 6 failed, 2 skipped, 15 subtests passed** in 90.54 seconds. One failure was caused by the audit wrapper disabling the config loader; that wrapper was corrected. Five tests assumed goal governance enabled while the repository's CI configuration disables it. A focused run with governance enabled and the corrected wrapper passed **23/23**. This is not a claim that the original CI configuration is green: the five configuration-dependent failures remain reproducible under that configuration.
- Frontend: **4/4 tests passed**; TypeScript check and Vite production build passed. `pip check` found no broken installed requirements. These are compatibility/build checks, not a dependency vulnerability audit.
- Completed an isolated PAPER BUY/SELL through the actual runtime queue, final gate, scheduler, charges, ledger, and fill callbacks: two fills, no remaining position, no unresolved orders. Supplied test prices were ₹1,000 and ₹1,010. Net simulated change was **+₹8.39**, after ₹1.07 charges and slippage. This deliberately chosen price move tests execution plumbing only; it is not strategy profit.
- Ran two synthetic generic backtests, six cached daily-bar equity runs, six fixed-parameter short-volatility runs with spread stress, and the repository's 27-variant NIFTY short-volatility gate evaluation without persisting or promoting results.
- Reproduced the closing-order safety bypass, limit-price breach, and lot-multiplier metrics error in isolated scripts. No live orders were submitted. No settings, positions, or kill-switch state in the running deployment were changed.

This was a broad, risk-focused audit with a whole-repository inventory and full existing tests. It was not a manual line-by-line proof of all 99,244 lines, a browser visual/interaction audit, an exchange conformance certification, or a fresh multi-session forward paper trial. The market was closed during the audit; the already-running agent remained subject to its normal schedule.

## Confirmed findings, ordered by urgency

**1. Critical — Caller metadata can bypass closing-order risk controls.**

`api/runtime.py:2189` copies caller metadata into an order. The final gate (`runtime.py:2567`), RiskEngine (`risk/engine.py:69`), and scheduler (`execution/scheduler.py:941`) trust `opens_position=False` without deriving whether the order reduces the actual held position. On a flat isolated account with the kill switch active, a BUY of 1,000 RELIANCE shares at ₹1,000, labelled closing with TARGET priority, was queued and filled. The account acquired **1,000 shares while the kill switch stayed active**. An ordinary entry was correctly rejected.

Fix: derive reducing quantity from signed holdings and outstanding reservations, reject or split any reversal/excess quantity, and recheck under the submission lock. Apply this to every submission path. Do not accept externally supplied hedging/closing flags as proof. The full bypass was tested in PAPER only; LIVE configuration/readiness gates were not bypassed or exercised against a real broker.

**2. High — The running control API has authentication disabled and is exposed on all host interfaces.**

The container has `API_AUTH_REQUIRED=false` and no configured API credential. An unauthenticated GET to the protected portfolio endpoint returned **200**. Docker publishes `0.0.0.0:8100`; network reachability beyond the host depends on Windows/firewall rules, which were not audited. `api/auth.py` bypasses the shared dependency in this configuration, so protected control mutations also lack application authentication. I did not send an unauthenticated mutation to the running deployment.

Fix: enable required authentication with a strong configured credential, and restrict the published interface/network. The explicitly configured `API_AUTH_REQUIRED=true` empty-credential case correctly fails closed; the unsafe deployed configuration is separate from that tested behavior.

**3. High — Costs are outdated, and the short-volatility cost calculation uses net structure premiums instead of all leg turnovers.**

`backtesting/charges.py:107` uses 0.0125% futures sell-side STT; line 120 uses 0.0625% option sell-premium STT. NSE specifies **0.05% and 0.15%, respectively, from 1 April 2026**. Current STT alone is understated by ₹375 on ₹10 lakh futures sell turnover and ₹87.50 on ₹1 lakh option sell-premium turnover. Historical evaluation needs date-effective schedules, not simply today's rate applied to every prior year. [NSE STT schedule](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax).

Separately, `backtesting/short_vol_backtest.py:285` estimates taxable sell/buy turnover from the net entry credit and net exit cost. A condor has four traded legs; net credit is not gross sell-premium turnover. Its brokerage formula also differs from the main flat-per-option-order calculation. Consequently, passing the current cost gate does not establish that all actual charges were covered. Fix with one date-aware per-leg charge engine and broker statement comparisons.

**4. High — Derivative round-trip metrics omit lot multipliers.**

`portfolio/ledger.py:130` stores `Trade.quantity` as lots, while `backtesting/metrics.py:50` multiplies the price change by that quantity without the instrument multiplier. A reproduced one-lot trade with lot size 50 and a ₹10 price gain produced **₹500 ledger profit but ₹10 in round_trip_pnls**. Costs are then subtracted in rupees, so this can change the sign of a trade and corrupt win rate/profit factor. The generic engine's ending equity and its trade statistics can disagree.

Fix: persist units and historical contract multiplier explicitly, migrate/qualify existing records, and reconcile closed-trade P&L to the ledger. Also resample equity to one observation per defined return interval before annualizing Sharpe: the current generic metric uses √252 on multiple mark-to-market samples per day (`metrics.py:96`).

**5. High — The generic backtester is not a trustworthy substitute for the running trading pipeline.**

The default data provider is synthetic (`backtesting/engine.py:80`), and it does not invoke the complete live master orchestrator/final runtime gate. Additional source-confirmed problems are:

- Current-bar closes mark the portfolio before the simulated opening decision and set the alleged session-opening equity (`engine.py:162–174`). Signal features exclude the current bar, but portfolio/risk state still sees information unavailable at the open.
- Option entry price is replaced with the underlying bar open (`engine.py:324`); option stop/target checks use underlying high/low against premium-denominated levels (`engine.py:193–221`). This mixes incompatible prices.
- All entries are INTRADAY products, yet exit holding periods can span multiple daily bars. Newly entered positions miss that day's exit check because exits are processed before entries. Stop fills use the stop level even when an opening gap has crossed it.

These defects make the generic backtest results diagnostic only, even when cached real underlying candles are supplied. Fix execution chronology, product holding rules, gap fills, and derivative price series before relying on its profit metrics.

**6. High — LIVE emergency reductions can be blocked by the kill-switch readiness check.**

The LIVE final gate calls overall readiness before its opening-only branches (`api/runtime.py:2628`). Overall readiness fails when the kill switch is active (`governance/live_readiness.py:270`). Thus a correctly classified closing order can pass the scheduler's reduction exception but still fail overall LIVE readiness. This is a source-traced concern; no LIVE broker test was performed.

Fix with a deliberate, authenticated reduction-only execution policy that retains broker/configuration checks while allowing verified reductions during the protective state. Do not solve it by weakening general live-order gates.

**7. High — Session-loss baselines use cost basis and reset at process startup.**

`api/runtime.py:1453` and `agent/trading_agent.py:437` seed session equity from `portfolio.equity`, which is explicitly cost-basis equity (`portfolio/ledger.py:140`), while later checks use marked equity. Existing unrealized gains/losses can be misclassified as today's change; a restart can also reset the loss budget. The deployment's latest kill-switch-clear event itself discusses a prior multi-day option loss recognized as a daily-loss event. That event is operator commentary, not proof of the precise cause.

Fix: persist the IST trading-session baseline using reconciled marks and retain it across intraday restarts. Audit daily P&L against previous-session equity plus cash movements.

**8. High — The exchange calendar is wrong, including the next day after this audit.**

`agent/market_hours.py:33` puts Holi on 25 March; the official 2026 F&O holiday is **3 March**. The app classifies **14 September 2026 as a trading day**, although NSE lists Ganesh Chaturthi as a holiday. It also blocks 4 November as Diwali while the published Laxmi Pujan date is 8 November. Additional holidays are missing. [NSE 2026 F&O holiday circular](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf), [NSE holiday notice](https://www.nseindia.com/resources/exchange-communication-holidays).

Fix: use versioned exchange/segment calendars, including expiry adjustments and special sessions. The running agent currently reports a next-open sleep consistent with the incorrect Monday assumption. Do not treat the holiday as an opportunity to generate paper trades from stale ticks.

**9. High — Short-volatility validation is narrower than the deployed trading configuration and is cost-sensitive.**

The visible passing gate evidence is an August NIFTY run. The running account automatically evaluates multiple indices/commodities in continuous mode. The research backtester uses model option premiums, fixed index specs (`short_vol_backtest.py:60`), daily closes, a five-trading-day holding horizon, and Monday entry defaults. These assumptions do not reproduce the entire current deployment, actual contract expiries, or executable option quotes. The shared short_vol strategy promotion ID does not establish evidence for every underlying/configuration.

Fix: bind promotion evidence to strategy version, instrument family, universe, parameters, data provenance and source revision. Evaluate each enabled deployment configuration on actual option-chain histories and a frozen forward paper record. The source acknowledges model-price and intraday-path limitations; the profit claims must reflect them.

**10. Medium — The backtest dashboard misstates its result and ignores its strategy filter.**

`hft_frontend/src/views/Backtest.tsx:57` sends `strategy_name`, while `api/runtime.py:1585` reads `strategy_names`. The chosen filter is ignored. The page calls the result historical simulation with a full trade log, although the default backend generates synthetic bars and returns aggregate orders, not a full trade log. The plotted curve contains only start and end values (`Backtest.tsx:70`), hiding the path and drawdowns.

Fix: use a typed shared request schema, expose source/interval/execution assumptions, return real equity and trade series, and add an interaction-level contract test. Four frontend tests are insufficient evidence for the dashboard's many workflows.

**11. Medium — Operational evidence for learning and promotion is incomplete.**

The live council skill endpoint has 17 traced decisions and 112 outcomes, but **zero joined observations**, reporting disjoint scan/order trace namespaces. It cannot validate whether council confidence predicts outcomes. The short_vol promotion gate reports **0 paper days** despite August–September paper fills: the runtime constructs the service without a paper-days lookup (`runtime.py:528`), so it falls back to metadata. Live-canary promotion remains blocked; that is protective, but the evidence collection is incomplete.

The source inventory also found 53 pass-only exception handlers across source/tests/scripts; some are legitimate cancellations, so this count is not 53 defects. One clear production example is `broker/simulated.py` silently falling back when live-price lookup fails, contrary to the repository's logging discipline. Fix the trace joins and provenance, derive qualifying paper sessions from immutable records, and make data-quality fallbacks observable.

## Historical experiments: all results, without choosing only winners

Every run below starts with ₹10 lakh. These are outputs of the current implementation, subject to the defects above; they are not return forecasts.

**Generic synthetic demos:** 30 bars produced −₹867.15 with 6 fills; 90 bars produced −₹1,761.96 with 56 fills. Synthetic prices provide execution examples, not evidence about NSE returns.

**Cached underlying daily bars, 23 August 2024–11 September 2026:** two unchanged strategies on three symbols, without tuning during this audit. Fill count includes entry and exit legs. Only INFY momentum was positive; five of six runs lost money. Capital usage differs from buy-and-hold, so these small account returns must not be presented as like-for-like benchmark excess returns.

| Symbol | Strategy | Fills | Account P&L | Reported profit factor |
|---|---|---:|---:|---:|
| RELIANCE | equity_momentum | 176 | ₹-5,218.05 | 0.727 |
| RELIANCE | swing_trend | 94 | ₹-888.26 | 0.935 |
| TCS | equity_momentum | 168 | ₹-795.62 | 0.953 |
| TCS | swing_trend | 98 | ₹-1,868.31 | 0.887 |
| INFY | equity_momentum | 156 | ₹1,597.69 | 1.082 |
| INFY | swing_trend | 92 | ₹-1,910.63 | 0.891 |

**Fixed default short-volatility strategy, approximately March 2019–August 2026:** underlying/VIX data are cached; option premiums are model-derived. Spread is points per leg on each side of the trade. These are default research settings, not a claim of matching every live deployment parameter.

| Underlying | Spread assumption | Structures closed | Win rate | Account P&L |
|---|---:|---:|---:|---:|
| NIFTY | 1 | 181 | 85.6% | ₹178,809.76 |
| NIFTY | 2 | 177 | 81.9% | ₹-52,873.21 |
| BANKNIFTY | 1 | 56 | 78.6% | ₹-17,551.73 |
| BANKNIFTY | 2 | 56 | 75.0% | ₹-18,856.40 |
| FINNIFTY | 1 | 64 | 73.4% | ₹-151,395.09 |
| FINNIFTY | 2 | 64 | 67.2% | ₹-190,459.74 |

The high win rates do not imply positive expectancy: FINNIFTY wins 73.4% under the baseline assumptions but loses about ₹1.51 lakh. NIFTY changes from profit to loss under the spread stress. The stress is illustrative execution-cost sensitivity, not a calibrated estimate of the actual spread distribution.

**Existing 27-variant NIFTY research sweep:** its DSR, PBO, Monte Carlo drawdown, cost and promotion checks all passed when rerun. The ex-post best final-equity variant reported +₹2,88,870 over 7.4 years, roughly **3.48% annualized**, with 136 closed structures. That best variant is selected from the tested sample, uses model prices and the flawed cost assumptions, and cannot be treated as an untouched forward result. A gate pass does not override observed deployment losses.

## What must happen before a defensible profit assessment

1. Repair and reproduce-test the closing-order bypass and reduction-only emergency path; secure the deployed API; correct session baselines and the exchange calendar.
2. Make ledger quantities, per-leg costs, historical contract specifications and equity/P&L measures reconcile. Correct the generic backtester and dashboard claims.
3. Freeze a specific strategy/configuration and validate it on untouched periods with executable option quotes, realistic costs/latency/rejections, benchmark comparisons, drawdowns and uncertainty. Do not reuse the same data for repeated selection and then label it unseen validation.
4. Accumulate the configured minimum of 30 qualifying paper sessions with immutable quote/order/fill/exit records, then continue until there are enough independent structures and market conditions to estimate expectancy and tail losses credibly. Thirty days alone does not establish profitability. Reconcile every session and retain restart/incident history.

**Profit conclusion:** the current observed paper account is down 3.41%; the historical evidence is mixed, model-dependent and fragile; several accounting/execution defects prevent stronger claims. The application may serve as a research and paper-testing platform after repairs. There is currently no valid basis to claim that it will consistently earn profits or approach its ₹5 crore annual target.

## Reproducible evidence files

- `deployment-readonly.json`: running app state, paper fills, equity samples, gates, promotion and AI evaluation; credential fields redacted.
- `exercise-results.json`, `exercise.py`: isolated round trip, generic/cached runs and initial probes.
- `probes-results.json`, `probes.py`: full simulated kill-switch bypass, lot-multiplier mismatch, fixed short-volatility stress runs.
- `shortvol-gates.log`: unpersisted 27-variant research gate evaluation.
- `backend-tests.log`, `backend-tests.xml`, `backend-followup.log`, `run_tests.py`: exact backend results and isolation setup.
- `frontend-tests.log`, `frontend-build.log`: frontend checks.
- `inventory.json`, `inventory.py`: repository/data inventory, not a claim of manual review of every line.

All added files are under `audit_results/2026-09-13`. No application source was changed or deployed. No broker trades were requested. No forward paper run was left running by this audit; the pre-existing deployed agent was left in its original operating state.
