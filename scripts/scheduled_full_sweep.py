"""Continuous multi-asset edge sweep — the scheduled version of the manual
2026-10-13 candidate sweep (reports/CANDIDATE_EDGE_SWEEP_2026-10-13.md).

WHY THIS EXISTS (2026-11-04):
The nightly TradingResearchLoop (scripts/scheduled_research_session.py) only
searches 5 instruments via LLM hypotheses. The broad research_*.py screen suite
— which actually covers stocks, MCX/commodities, index futures basis, intraday,
gap and FX — was only ever run BY HAND (last: 2026-10-13). So a newly-emerging
edge in any of those asset classes would not be caught between manual sweeps.
This runner re-screens every asset class on a schedule and routes any candidate
that reports an edge into data/research_sessions/needs_review.log — the SAME
human-review queue the nightly loop uses.

IT NEVER PROMOTES OR TRADES ANYTHING. A screen pass is a LEAD to validate, not a
tradeable edge: the screens use optimistic flat costs and (for basis/overlay)
single-contract or synthetic-continuous series. The gap signal passed its screen
AND the flat-cost gate suite, then died under a realistic execution-cost model
(CANDIDATE_EDGE_SWEEP_2026-10-13.md). So a needs_review.log entry means "a human
/ Claude must run the full gates + realistic execution model and decide", never
"start trading this".

Usage:
    python -m scripts.scheduled_full_sweep
    python -m scripts.scheduled_full_sweep --timeout 420
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_LOG_DIR = _REPO / "data" / "research_sessions"
_NEEDS_REVIEW = _LOG_DIR / "needs_review.log"

# The research SCREENS to run, grouped by asset class. Each is a standalone
# script that logs a "VERDICT: ..." line and exits 0 when it finds a candidate
# edge. usdinr_overlay is included (its stale-token bug was fixed 2026-11-04).
# Deliberately excludes one-off gated runners (run_gap_gates*.py) and the LLM
# loop (scheduled_research_session.py) — those are driven separately.
_SCREENS: dict[str, list[str]] = {
    "stocks": [
        "research_classic_momentum.py",
        "research_cross_sectional.py",
        "research_sector_rotation.py",
        "research_turn_of_month.py",
        "research_lowvol_reversal.py",
    ],
    "mcx_commodities": [
        "research_commodity_carry.py",
        "research_commodity_pairs.py",
        "research_intraday_commodity_trend.py",
    ],
    "index_and_vol": [
        "research_index_basis.py",
        "research_premium_regime_filter.py",
        "research_vol_premium.py",
    ],
    "intraday": [
        "research_intraday_edge.py",
        "research_intraday_backtest.py",
        "research_intraday_vwap_pinning.py",
    ],
    "gap": [
        "research_gap_backtest.py",
        "research_gap_calendar_volume.py",
    ],
    "fx": [
        "research_usdinr_overlay.py",
    ],
}

# A VERDICT line counts as a candidate when it says an edge was found. The
# screens phrase a positive verdict as "edge found" / "worth building"; a
# negative one as "no tradeable edge" / "no edge". We match the positive
# phrasing and explicitly exclude the negatives so wording drift fails safe
# (unknown wording -> treated as NOT a candidate, i.e. not escalated).
_EDGE_RE = re.compile(r"edge found|worth building", re.IGNORECASE)
_NO_EDGE_RE = re.compile(r"no tradeable edge|no edge|insufficient", re.IGNORECASE)


def _verdict_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if "VERDICT" in ln.upper()]
    return lines[-1] if lines else ""


def _is_candidate(verdict: str, exit_code: int) -> bool:
    if not verdict:
        return False
    if _NO_EDGE_RE.search(verdict):
        return False
    return bool(_EDGE_RE.search(verdict)) and exit_code == 0


def run_screen(script: str, timeout: int) -> dict:
    path = _REPO / "scripts" / script
    if not path.exists():
        return {"script": script, "status": "missing", "verdict": "", "candidate": False}
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"script": script, "status": "timeout", "verdict": "", "candidate": False}
    except Exception as exc:  # a broken screen must not abort the whole sweep
        return {"script": script, "status": f"error: {exc}", "verdict": "", "candidate": False}
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    verdict = _verdict_line(output)
    candidate = _is_candidate(verdict, proc.returncode)
    return {
        "script": script,
        "status": f"exit {proc.returncode}",
        "verdict": verdict or "(no VERDICT line)",
        "candidate": candidate,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=420, help="per-screen timeout (s)")
    args = ap.parse_args()

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    summary_path = _LOG_DIR / f"full_sweep_{started:%Y%m%d_%H%M%S}.log"

    results: list[dict] = []
    candidates: list[dict] = []
    for asset_class, scripts in _SCREENS.items():
        for script in scripts:
            res = run_screen(script, args.timeout)
            res["asset_class"] = asset_class
            results.append(res)
            tag = "CANDIDATE" if res["candidate"] else "no-edge "
            print(f"[{tag}] {asset_class:16} {script:38} {res['status']:9} {res['verdict']}")
            if res["candidate"]:
                candidates.append(res)

    lines = [f"# Full multi-asset edge sweep — {started.isoformat()}", ""]
    for r in results:
        lines.append(f"[{'CANDIDATE' if r['candidate'] else 'no-edge'}] "
                     f"{r['asset_class']} / {r['script']} — {r['status']} — {r['verdict']}")
    lines.append("")
    lines.append(f"{len(candidates)} candidate(s) need validation (full gates + realistic execution).")
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    if candidates:
        with _NEEDS_REVIEW.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== full_sweep {started.isoformat()} — {len(candidates)} screen candidate(s) "
                     f"(VALIDATE, do not trade) ===\n")
            for c in candidates:
                fh.write(f"  [{c['asset_class']}] {c['script']}: {c['verdict']}\n")
            fh.write("  NEXT: run the full gate suite + realistic execution-cost model "
                     "(see scripts/run_gap_gates_realistic.py) before any promotion.\n")

    print(f"\nSweep complete: {len(results)} screens, {len(candidates)} candidate(s). "
          f"Summary: {summary_path.name}"
          + (f"; escalated to {_NEEDS_REVIEW.name}" if candidates else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
