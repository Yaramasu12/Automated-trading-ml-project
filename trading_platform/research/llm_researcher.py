"""LLM-driven edge research: the local model PROPOSES, the harness DISPOSES.

THE ONE DESIGN DECISION THAT MATTERS
-------------------------------------
An LLM cannot find statistical edge by looking at price data. It is a pattern
completer over text, not an estimator; asked "what does this price series
predict?" it will produce fluent, confident, worthless answers. Any design that
treats the model as an oracle over market data is building the exact "fake edge
presented as intelligence" failure this repo's CLAUDE.md opens by warning about.

What an LLM IS good at is *generating candidate hypotheses* — it has absorbed
the quant literature and can write a hundred plausible strategies faster than a
human can write one. That weakness (cannot validate) is precisely covered by
the harness's strength (validates rigorously and cheaply).

So the contract is strictly:

    local LLM  ->  proposes a hypothesis as code
    harness    ->  backtests it, gates it, returns a verdict
    LLM        ->  sees the verdict, proposes the next one

The model never decides what is true. It only decides what to try next.

THE TRAP THIS CREATES, AND THE DEFENCE
---------------------------------------
Automated proposal makes it trivial to run hundreds of hypotheses. Test enough
and something clears any fixed threshold **by luck alone** — at PBO<=0.4 and
DSR>=0.5, roughly 1 in 20 pure-noise strategies will pass. An LLM loop that
iterates until something passes is a machine for manufacturing false positives,
and it would feel like progress the whole way.

Three defences, all enforced here rather than left to discipline:

1. **HOLDOUT.** The final slice of history is split off before research starts
   and the LLM is NEVER shown results on it. A hypothesis that passes the
   research period is re-run once on holdout; disagreement means rejected.
2. **Attempt counting.** Every proposal is counted. `ResearchSession.report()`
   states the count and the implied false-discovery expectation, so "we found
   one!" can always be read against "out of how many?".
3. **No threshold tuning.** The gates keep the same thresholds short-vol and
   trend-following faced. Loosening them to get a pass is the failure mode this
   whole apparatus exists to prevent.

Expect ~all proposals to fail. That is the correct outcome for daily bars on a
73%-algo market, and it is what the harness already demonstrated by rejecting
momentum, breakout and mean-reversion.
"""
from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from trading_platform.backtesting.short_vol_backtest import DailyBar
from trading_platform.research.hypothesis_harness import (
    HypothesisHarness,
    HypothesisSpec,
    HypothesisVerdict,
    buy_and_hold_cagr,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:1234/v1"
# Instruct-tuned, not a reasoning model: reasoning models (qwq, deepseek-r1)
# emit long chains of thought and, uncapped, run until LM Studio terminates
# the request ({"error":"terminated"}). This task wants a short structured
# reply, not deliberation.
DEFAULT_MODEL = "meta/llama-3.3-70b"

SYSTEM_PROMPT = """You are a quantitative researcher proposing candidate trading edges.

You will be given: the instrument, the data available, and the results of every
hypothesis already tried. Propose ONE new hypothesis, as a Python function.

CONTRACT — your function MUST match this exactly:

    def hypothesis(bars, params):
        # bars: list of objects with .day (datetime.date) and .close (float),
        #       in chronological order.
        # params: dict of the parameters you declare in PARAMS.
        # RETURN: list[float] of len(bars) — desired exposure per bar,
        #         +1.0 = fully long, 0.0 = flat, -1.0 = fully short.
        ...

RULES:
- Return EXACTLY len(bars) values.
- Element i may use ONLY bars[0..i]. Using bars[i+1] or later is LOOKAHEAD and
  will be rejected — it is the most common way to fake a result.
- Pure Python plus `math`. No imports, no file/network access.
- Declare a small parameter grid (2-5 values for 1-2 parameters). Keep it
  small and honest: these values are used to deflate your result for
  selection bias, so padding the grid hurts you.

Reply in EXACTLY this format, nothing else:

NAME: short_snake_case_name
RATIONALE: one or two sentences on the economic reason this could work
PARAMS: {"param_name": [v1, v2, v3]}
CODE:
```python
def hypothesis(bars, params):
    ...
```
"""


@dataclass
class Proposal:
    name: str
    rationale: str
    code: str
    param_grid: list[dict]
    raw: str = ""


@dataclass
class ResearchSession:
    instrument: str
    attempts: list[tuple[Proposal, HypothesisVerdict | None]] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    holdout_results: dict[str, HypothesisVerdict] = field(default_factory=dict)

    @property
    def n_attempts(self) -> int:
        return len(self.attempts)

    def survivors(self) -> list[tuple[Proposal, HypothesisVerdict]]:
        return [(p, v) for p, v in self.attempts if v is not None and v.passed]

    def report(self) -> str:
        lines = [
            f"LLM research session — {self.instrument}",
            f"hypotheses proposed : {self.n_attempts}",
            f"passed research gates: {len(self.survivors())}",
        ]
        # Multiple comparisons, stated rather than assumed away.
        if self.n_attempts:
            expected_false = self.n_attempts * 0.05
            lines.append(
                f"expected false positives at ~5% : {expected_false:.1f} "
                f"— a single passing result out of {self.n_attempts} is NOT evidence"
            )
        for prop, verdict in self.attempts:
            if verdict is None:
                lines.append(f"  {prop.name:28} UNRUNNABLE")
                continue
            mark = "PASS" if verdict.passed else "fail"
            bench = ""
            if verdict.beats_benchmark() is not None:
                bench = " | beats-benchmark" if verdict.beats_benchmark() else " | loses-to-benchmark"
            lines.append(
                f"  {prop.name:28} {mark}  CAGR {verdict.best_cagr * 100:6.2f}%  "
                f"Sharpe {verdict.best_sharpe:5.2f}{bench}"
            )
        for name, hv in self.holdout_results.items():
            lines.append(
                f"  HOLDOUT {name:20} {'PASS' if hv.passed else 'FAIL'}  "
                f"CAGR {hv.best_cagr * 100:6.2f}%  Sharpe {hv.best_sharpe:5.2f}"
            )
        return "\n".join(lines)


class LocalLLMClient:
    """Minimal OpenAI-compatible client for LM Studio / Ollama."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                 timeout: int = 180, max_tokens: int = 1200) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # Bounded so a verbose model cannot run until the server kills the
        # request. A proposal is ~30 lines; 1200 tokens is ample.
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str, temperature: float = 0.7) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # LM Studio puts the actual reason (bad model id, context overflow,
            # unloaded model) in the BODY. Surfacing only the status turns every
            # one of those into an indistinguishable "HTTP Error 400".
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise RuntimeError(f"local LLM HTTP {exc.code}: {detail}") from exc
        return data["choices"][0]["message"]["content"]


def parse_proposal(text: str) -> Proposal | None:
    """Extract a proposal from the model's reply. Returns None if malformed —
    a garbled reply is skipped, never guessed at."""
    name_m = re.search(r"NAME:\s*(\w+)", text)
    rat_m = re.search(r"RATIONALE:\s*(.+)", text)
    params_m = re.search(r"PARAMS:\s*(\{.*?\})", text, re.DOTALL)
    code_m = re.search(r"```python\s*(.*?)```", text, re.DOTALL)
    if not (name_m and code_m):
        return None
    grid: list[dict] = [{}]
    if params_m:
        try:
            raw = json.loads(params_m.group(1))
            keys = list(raw)
            if keys:
                grid = []
                # Cartesian product across declared parameters.
                def expand(i: int, acc: dict) -> None:
                    if i == len(keys):
                        grid.append(dict(acc)); return
                    for v in raw[keys[i]]:
                        acc[keys[i]] = v
                        expand(i + 1, acc)
                expand(0, {})
        except (json.JSONDecodeError, TypeError):
            grid = [{}]
    return Proposal(
        name=name_m.group(1).strip(),
        rationale=(rat_m.group(1).strip() if rat_m else ""),
        code=code_m.group(1).strip(),
        param_grid=grid[:12],          # cap the searched space
        raw=text,
    )


# Names the generated function may touch. Deliberately tiny: this is numeric
# code over a price list, so it needs arithmetic and nothing else. Not an
# adversarial sandbox (the model is local and trusted-ish) — it exists to stop
# an LLM that hallucinated `import os` from doing something surprising, and to
# make lookahead the only thing left to worry about.
_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "range": range,
    "float": float, "int": int, "round": round, "sorted": sorted, "enumerate": enumerate,
    "zip": zip, "list": list, "any": any, "all": all, "pow": pow, "print": print,
}


def compile_hypothesis(code: str):
    """Compile generated code into a callable exposure function."""
    if re.search(r"\b(import|__|open|eval|exec|compile|globals|locals)\b", code):
        raise ValueError("generated code contains a forbidden construct")
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS, "math": math}
    exec(compile(code, "<llm_hypothesis>", "exec"), namespace)  # noqa: S102
    fn = namespace.get("hypothesis")
    if not callable(fn):
        raise ValueError("generated code defines no `hypothesis` function")
    return fn


class LLMResearcher:
    """Runs propose -> validate -> feed-back loops against the local model."""

    def __init__(
        self,
        client: LocalLLMClient | None = None,
        harness: HypothesisHarness | None = None,
        *,
        holdout_frac: float = 0.30,
    ) -> None:
        self.client = client or LocalLLMClient()
        self.harness = harness or HypothesisHarness()
        self.holdout_frac = holdout_frac

    def _prompt(self, instrument: str, bars: Sequence[DailyBar],
                session: ResearchSession, benchmark: float) -> str:
        lines = [
            f"Instrument: {instrument}",
            f"Data: {len(bars)} daily bars, {bars[0].day} to {bars[-1].day}.",
            f"Buy-and-hold over this period returned {benchmark * 100:.2f}%/yr — "
            f"a hypothesis that cannot beat this is not worth trading.",
            "",
            "Known result: classic daily-bar technical strategies have been tested on this "
            "data and ALL failed with high probability-of-backtest-overfitting "
            "(price momentum PBO 0.96, donchian breakout 0.51, mean reversion 0.87). "
            "Simple moving-average and breakout rules are exhausted; propose something "
            "structurally different.",
        ]
        if session.attempts:
            lines.append("\nAlready tried in this session:")
            for prop, verdict in session.attempts:
                if verdict is None:
                    lines.append(f"- {prop.name}: could not be run")
                else:
                    lines.append(
                        f"- {prop.name}: {'PASSED' if verdict.passed else 'FAILED'}, "
                        f"CAGR {verdict.best_cagr * 100:.2f}%, Sharpe {verdict.best_sharpe:.2f}"
                        + (f", PBO {verdict.gates.pbo.metric:.2f}"
                           if verdict.gates.pbo is not None else "")
                    )
            lines.append("\nDo NOT repeat these. Propose a genuinely different idea.")
        return "\n".join(lines)

    def research(
        self,
        instrument: str,
        bars: Sequence[DailyBar],
        *,
        rounds: int = 5,
        verbose: bool = True,
    ) -> ResearchSession:
        # Split BEFORE any research happens. The model never sees holdout
        # results, so a hypothesis cannot be tuned to them even accidentally.
        split = int(len(bars) * (1 - self.holdout_frac))
        research_bars, holdout_bars = list(bars[:split]), list(bars[split:])
        benchmark = buy_and_hold_cagr(research_bars)
        session = ResearchSession(instrument=instrument)

        for rnd in range(rounds):
            try:
                reply = self.client.complete(
                    SYSTEM_PROMPT, self._prompt(instrument, research_bars, session, benchmark)
                )
            except (urllib.error.URLError, OSError, KeyError) as exc:
                logger.warning("local LLM call failed on round %d: %s", rnd + 1, exc)
                break

            proposal = parse_proposal(reply)
            if proposal is None:
                logger.info("round %d: unparseable reply, skipped", rnd + 1)
                continue
            try:
                fn = compile_hypothesis(proposal.code)
                spec = HypothesisSpec(
                    name=proposal.name,
                    exposure_fn=lambda b, p, _fn=fn: _fn(b, p),
                    param_grid=proposal.param_grid,
                    description=proposal.rationale,
                )
                verdict = self.harness.evaluate(spec, research_bars, benchmark_cagr=benchmark)
            except Exception as exc:
                logger.info("round %d: %s unrunnable: %s", rnd + 1, proposal.name, exc)
                session.attempts.append((proposal, None))
                continue

            session.attempts.append((proposal, verdict))
            if verbose:
                print(f"[{rnd + 1}/{rounds}] {proposal.name}: "
                      f"{'PASS' if verdict.passed else 'fail'} "
                      f"CAGR {verdict.best_cagr * 100:.2f}% Sharpe {verdict.best_sharpe:.2f}")

        # Anything that survived research gets exactly ONE holdout run.
        for prop, verdict in session.survivors():
            try:
                fn = compile_hypothesis(prop.code)
                spec = HypothesisSpec(prop.name, lambda b, p, _fn=fn: _fn(b, p), prop.param_grid)
                session.holdout_results[prop.name] = self.harness.evaluate(
                    spec, holdout_bars, benchmark_cagr=buy_and_hold_cagr(holdout_bars)
                )
            except Exception as exc:
                logger.warning("holdout run failed for %s: %s", prop.name, exc)
        return session
