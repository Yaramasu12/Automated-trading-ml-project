# Trading Risks & Caveats — READ BEFORE LIVE TRADING

**This document is NOT financial or legal advice. This is technical documentation for educational purposes.**

## ⚠️ Critical Safety Warnings

### 1. Money Loss Risk

- Automated trading systems can lose money **rapidly** — thousands of dollars in seconds
- Leverage (options, futures) can amplify losses **beyond your initial capital**
- A single bug can cause catastrophic losses before any human can react
- **Always start with paper trading for a minimum of 4-8 weeks before considering live**

### 2. Live Trading Requires TWO Env Vars

```bash
# This is INTENTIONALLY hard to trigger accidentally:
MODE=live CONFIRM_LIVE=I_UNDERSTAND python -m trading_platform.live
```

- Both `MODE=live` AND `CONFIRM_LIVE=I_UNDERSTAND` must be set
- Without both, the system **MUST** refuse to place live orders
- The default is `MODE=paper` — you cannot accidentally trigger live trading

### 3. Global Kill Switch

The kill switch is a **first-class safety mechanism**, not an afterthought:

```python
# Check kill switch before every order
if kill_switch.is_active():
    logger.critical("KILL SWITCH ACTIVE — halting all trading")
    square_open_positions()
    sys.exit(1)
```

- Set `KILL_SWITCH_ENABLED=true` to activate
- There is also a **physical/manual path** (file-based flag) that cannot be bypassed by software bugs
- Monitor the kill-switch status in Grafana dashboards

## Technical Risks

### 4. Overfitting is the Default Failure Mode

- A backtest that looks great is **usually wrong**
- Insist on **out-of-sample** and **walk-forward** validation
- Use **realistic transaction costs** and **slippage** models
- Long **paper trading** before real capital is mandatory

### 5. Model Drift

- ML models degrade over time as market conditions change
- Monitor for drift using Evidently ML monitoring
- Set automatic retraining triggers based on performance degradation
- No ML model should trade without being within its **validity window**

### 6. LLM/AI Hallucination

- LLMs **hallucinate** — they make things up confidently
- All AI outputs are **advisory only** — they never place orders directly
- AI signals pass through the same deterministic risk gates as any other signal
- Keep AI in the research layer, not the execution layer

### 7. Data Quality

- Bad or gapped data produces **confidently wrong signals**
- The system includes gap detection and heartbeats
- Missing data should trigger **conservative defaults** (reduce position size)
- Always verify data completeness before trading

### 8. Broker API Risks

- IBKR Gateway is **socket-based** and can crash/disconnect
- The system runs IB Gateway in a container with automatic restart
- **Reconciliation on every start-up** ensures order/position state matches
- Heartbeat monitoring detects silent failures

### 9. Automation Amplifies Bugs

- A runaway loop can fire **thousands of orders in seconds**
- Rate limits (`MAX_ORDERS_PER_SECOND`) prevent order storms
- Duplicate-order suppression prevents accidental re-submission
- Price collars prevent fat-finger errors

### 10. Operational Risks

- Disk full → data corruption → silent losses
- Memory leak → OOM kills → missed signals
- Network partition → disconnected broker → stale positions
- **Monitoring and alerting exist to catch these** — configure them

## Regulatory Considerations

### Pattern Day Trader (PDT) Rules

- If you have < $25,000 in your account, you're limited to **3 day trades per rolling 5 business days**
- The system must enforce PDT rules in the risk layer
- `MAX_DAILY_TRADES` limit is config-driven

### Tax Implications

- Every trade may trigger a taxable event (depending on jurisdiction)
- Wash-sale rules apply to **substantially identical** securities
- Keep detailed records for tax reporting

### Market Data Licensing

- Real-time market data requires **exchange licenses**
- Free/educational data may have **distribution restrictions**
- Databento, Polygon, and others have different licensing terms
- **Consult legal before distributing any system using market data**

### Trading Others' Money

- Trading **your own capital** is generally unregulated as an activity
- Trading **others' money** triggers **registration obligations** (RIA, etc.)
- **Consult a professional before taking external capital**

## Sub-50µs HFT Reality Check

**Competing on pure speed against co-located market-makers is NOT achievable through:**

- A retail broker API (IBKR adds 50-200ms round-trip minimum)
- A system running on commodity hardware
- Python code (even with Cython/Numba)

**True HFT requires:**

- Exchange co-location (physical proximity to matching engines)
- Direct market data feeds (bypassing brokers)
- Kernel-bypass NICs (Solarflare/Mellanox)
- FPGA or ASIC hardware
- Annual costs of **$100K+** before a single trade

**This system targets the "low-latency tier" (1-50ms), not true HFT.** This is actually where most achievable edge exists for small teams.

## Pre-Live Checklist

Before deploying to a live account:

- [ ] Paper trading for minimum 4-8 weeks
- [ ] All tests passing (`pytest tests/ -v`)
- [ ] CI pipeline green
- [ ] Kill switch tested (deliberately triggered in paper)
- [ ] Grafana dashboards monitoring latency, PnL, exposure
- [ ] Alertmanager configured for critical events
- [ ] Sentry configured for error tracking
- [ ] Backup/restore procedure tested
- [ ] Recovery procedure documented and tested
- [ ] Position reconciliation verified on restart
- [ ] Drawdown auto-flatten tested
- [ ] Audit log verified (every decision logged)
- [ ] Data heartbeat monitoring active
- [ ] Broker gateway heartbeat monitoring active
- [ ] Disk space monitoring configured
- [ ] Memory monitoring configured
- [ ] Network connectivity monitoring configured
- [ ] **Reviewed by an experienced trader**
- [ ] **Reviewed by legal/tax professional**

## Emergency Procedures

### How to Stop Trading Immediately

1. **Kill switch**: Set `KILL_SWITCH_ENABLED=true` or touch `deploy/.kill_switch` file
2. **Docker**: `docker compose stop`
3. **Emergency**: Shut down the machine/container

### How to Recover After Crash

1. Start all services: `docker compose up -d`
2. Run reconciliation: `python -m trading_platform.execution.reconciliation`
3. Compare positions against broker
4. If mismatch, review audit log
5. Manually square positions if needed

### Contact Information

- **Sentry**: Check https://sentry.io for error reports
- **Alertmanager**: Check webhook destination for alerts
- **Grafana**: https://grafana-host for real-time dashboards
- **Audit Log**: `data/audit/` directory for every decision

---

**By using this system, you acknowledge that you have read and understood these risks. You are responsible for your own trading decisions and their consequences.**