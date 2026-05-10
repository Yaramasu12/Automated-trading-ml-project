# Policy Promotion

## Promotion Lifecycle

```
research → shadow → paper → live_canary → live_approved
```

Any status can transition to `disabled` immediately.

## Requirements per Stage

| Gate | shadow | paper | live_canary | live_approved |
|------|--------|-------|-------------|---------------|
| Tests pass | ✓ | ✓ | ✓ | ✓ |
| Data quality | ✓ | ✓ | ✓ | ✓ |
| OOS profit factor ≥ 1.10 | ✓ | ✓ | ✓ | ✓ |
| Sharpe ≥ 0.20 | ✓ | ✓ | ✓ | ✓ |
| Max drawdown pass | ✓ | ✓ | ✓ | ✓ |
| Slippage sensitivity | ✓ | ✓ | ✓ | ✓ |
| Paper/canary results | — | — | ✓ | ✓ |
| Manual approval | — | — | — | ✓ |

## Rollback

Every promotion stores a rollback pointer. Use `POST /policies/rollback` to revert.

## API

```
GET  /policies             — list all policies
POST /policies/promote     — promote { policy_id, status }
POST /policies/rollback    — rollback { policy_id }
```
