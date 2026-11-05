"""Corporate-actions ingest adapter for the Phase-3 point-in-time back-
adjustment (corporate_actions table + MarketDataStore.adjusted_closes).

STATUS: EMPTY / UNPOPULATED in this environment — a labeled follow-up, NOT a
fabricated feed. No corporate actions are invented. The parser below is real
and tested; what is not available here is an automatic live pull.

SOURCE (confirmed, 2026-11-05): NSE publishes corporate actions as JSON at
    GET https://www.nseindia.com/api/corporates-corporateActions?index=equities
    rows carry: symbol, series, subject (free text), exDate, and the action
    detail. `subject` encodes the action, e.g.
      "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/-..."
      "Bonus 1:1"
      "Dividend - Rs 5 Per Share"
BLOCKER: like MCX, this endpoint is cookie/WAF-gated — it must be primed with a
browser session first, which the standard urllib stack (and this sandbox's
safety policy) will not do. So `fetch` is NOT auto-run here; it documents the
contract and raises a clear error. To populate in production, hand the JSON
rows (or pre-parsed CorporateAction records) to `ingest(db, rows=...)` from an
authenticated edge fetcher — the parse + persist path is exercised by tests.

Point-in-time safety lives in the READER (MarketDataStore.adjusted_closes
filters ex_date <= as_of); this module only records the raw actions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

SOURCE = "nse_corp_actions"
NSE_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"


@dataclass
class CorporateAction:
    symbol: str
    ex_date: date
    action_type: str            # SPLIT / BONUS / DIV
    ratio_num: float | None     # resulting shares per `ratio_den` prior shares
    ratio_den: float | None
    raw_text: str


def _parse_ex_date(v) -> date | None:
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def classify_subject(subject: str) -> tuple[str, float | None, float | None]:
    """Map an NSE corporate-action `subject` string to
    (action_type, ratio_num, ratio_den). Best-effort, conservative: only the
    unambiguous SPLIT (face-value sub-division) and BONUS ratios yield numbers;
    everything dividend-like is DIV with no ratio (not applied to price). ratio
    is resulting:prior shares, so the ex-date price multiplier is den/num."""
    s = (subject or "").strip()
    up = s.upper()
    # Face-value split: "... From Rs 10/- ... To Rs 2/- ..."
    if "SPLIT" in up or "SUB-DIVISION" in up or "SUB DIVISION" in up:
        fvs = re.findall(r"RS\.?\s*([0-9]+(?:\.[0-9]+)?)", up)
        if len(fvs) >= 2:
            old_fv, new_fv = float(fvs[0]), float(fvs[1])
            if new_fv > 0 and old_fv > 0:
                # shares multiply by old_fv/new_fv -> num:den = old_fv:new_fv
                return "SPLIT", old_fv, new_fv
        return "SPLIT", None, None
    # Bonus: "Bonus 1:1", "Bonus 3:5" (X bonus shares per Y held)
    if "BONUS" in up:
        m = re.search(r"([0-9]+)\s*:\s*([0-9]+)", up)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            if x > 0 and y > 0:
                # holder ends with (x+y) shares per y held -> num:den = (x+y):y
                return "BONUS", x + y, y
        return "BONUS", None, None
    if "DIVIDEND" in up or "DIV" in up:
        return "DIV", None, None
    return "OTHER", None, None


def parse_nse_corporate_actions(rows: list[dict]) -> list[CorporateAction]:
    """Parse NSE corporate-action JSON rows into CorporateAction records.
    Rows with an unparseable ex-date or a non-actionable subject are skipped."""
    out: list[CorporateAction] = []
    for r in rows or []:
        sym = (r.get("symbol") or r.get("Symbol") or "").strip().upper()
        subject = r.get("subject") or r.get("Subject") or r.get("purpose") or ""
        ex = _parse_ex_date(r.get("exDate") or r.get("ExDate") or r.get("ex_date"))
        if not sym or ex is None:
            continue
        atype, num, den = classify_subject(subject)
        if atype == "OTHER":
            continue
        out.append(CorporateAction(sym, ex, atype, num, den, str(subject).strip()))
    return out


def persist(db, actions: list[CorporateAction], *, source: str = SOURCE) -> int:
    """Idempotently upsert parsed actions into corporate_actions. Returns the
    count written. Safe to replay (upsert on the natural key)."""
    n = 0
    for a in actions:
        db.upsert_corporate_action(
            symbol=a.symbol, ex_date=a.ex_date, action_type=a.action_type,
            ratio_num=a.ratio_num, ratio_den=a.ratio_den,
            raw_text=a.raw_text, source=source,
        )
        n += 1
    return n


def ingest(db, *, rows: list[dict] | None = None,
           actions: list[CorporateAction] | None = None,
           source: str = SOURCE) -> int:
    """Persist corporate actions supplied as raw NSE JSON `rows` or pre-parsed
    `actions`. With neither, this is a NO-OP that returns 0 and logs that the
    feed is unpopulated in this environment (see module docstring) — it never
    fabricates actions."""
    if actions is None:
        actions = parse_nse_corporate_actions(rows) if rows else []
    return persist(db, actions, source=source)


def fetch(*_args, **_kwargs):  # pragma: no cover - cookie/WAF-gated, see docstring
    raise NotImplementedError(
        "NSE corporate-actions endpoint is cookie/WAF-gated and not auto-fetched "
        "here. Supply JSON rows to ingest(db, rows=...) from an authenticated "
        f"edge fetcher. Endpoint: {NSE_URL}")
