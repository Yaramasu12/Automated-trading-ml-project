from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
)
from trading_platform.domain.models import Instrument


INDEX_UNDERLYINGS = {
    # NSE indices — F&O on NFO, weekly expiry on Thursday
    # Synthetic bases are deliberately stable for local backtests/tests.
    # Production symbols, tokens, strikes, and expiries are refreshed from the
    # Angel One instrument master before live or paper-shadow operation.
    "NIFTY":      {"token": "26000", "lot_size": 50,  "strike_step": 50,  "base": 22500},
    "BANKNIFTY":  {"token": "26009", "lot_size": 15,  "strike_step": 100, "base": 48500},
    "FINNIFTY":   {"token": "26037", "lot_size": 40,  "strike_step": 50,  "base": 23000},
    "MIDCPNIFTY": {"token": "26074", "lot_size": 75,  "strike_step": 25,  "base": 12500},
    # BSE indices — F&O on BFO; SENSEX weekly expires Friday, BANKEX weekly expires Monday
    "SENSEX": {"token": "1",  "exchange": "BSE", "expiry_day": "friday",  "lot_size": 10, "strike_step": 100, "base": 80000},
    "BANKEX": {"token": "12", "exchange": "BSE", "expiry_day": "monday",  "lot_size": 15, "strike_step": 100, "base": 58000},
}

# Tokens sourced from Angel One NSE instrument master (NSE Cash Market)
LIQUID_EQUITIES = {
    # Nifty 50 — core
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    # Nifty 50 — extended
    "WIPRO": "3787",
    "KOTAKBANK": "1922",
    "AXISBANK": "5900",
    "MARUTI": "10999",
    "SUNPHARMA": "3351",
    "TATAMOTORS": "3456",
    "BAJFINANCE": "317",
    "HINDUNILVR": "1394",
    "ASIANPAINT": "236",
    "LTIM": "17818",
    "BHARTIARTL": "10604",
    "ONGC": "2475",
    "NTPC": "11630",
    "POWERGRID": "14977",
    "TITAN": "3506",
    # Nifty 100 additions (tokens refreshed from Angel One instrument master at pre-market)
    "ITC": "1660",
    "LT": "11483",
    "HCLTECH": "7229",
    "M&M": "2031",
    "COALINDIA": "20374",
    "HEROMOTOCO": "1348",
    "HINDALCO": "1363",
    "JSWSTEEL": "11723",
    "ULTRACEMCO": "2629",
    "GRASIM": "1232",
    "BPCL": "526",
    "CIPLA": "694",
    "DRREDDY": "881",
    "EICHERMOT": "910",
    "ADANIENT": "25",
    "ADANIPORTS": "15083",
    "APOLLOHOSP": "157",
    "TATACONSUM": "3432",
    "TRENT": "1964",
    "BAJAJFINSV": "16675",
    "DIVISLAB": "10940",
    "SHRIRAMFIN": "4306",
}

EQUITY_FO_UNDERLYINGS = {
    # ── Nifty 50 core ────────────────────────────────────────────────────────
    "RELIANCE":   {"lot_size": 250,  "strike_step": 20,  "base": 2800},
    "TCS":        {"lot_size": 175,  "strike_step": 50,  "base": 3500},
    "INFY":       {"lot_size": 400,  "strike_step": 20,  "base": 1500},
    "HDFCBANK":   {"lot_size": 550,  "strike_step": 20,  "base": 1600},
    "ICICIBANK":  {"lot_size": 700,  "strike_step": 10,  "base": 1100},
    "SBIN":       {"lot_size": 750,  "strike_step": 10,  "base": 750},
    # ── Nifty 50 extended ────────────────────────────────────────────────────
    "WIPRO":      {"lot_size": 1500, "strike_step": 10,  "base": 450},
    "KOTAKBANK":  {"lot_size": 400,  "strike_step": 20,  "base": 1750},
    "AXISBANK":   {"lot_size": 1200, "strike_step": 10,  "base": 1050},
    "MARUTI":     {"lot_size": 100,  "strike_step": 100, "base": 11000},
    "SUNPHARMA":  {"lot_size": 700,  "strike_step": 10,  "base": 1650},
    "TATAMOTORS": {"lot_size": 1500, "strike_step": 5,   "base": 750},
    "BAJFINANCE": {"lot_size": 125,  "strike_step": 100, "base": 7000},
    "HINDUNILVR": {"lot_size": 300,  "strike_step": 20,  "base": 2300},
    "BHARTIARTL": {"lot_size": 950,  "strike_step": 10,  "base": 1600},
    "NTPC":       {"lot_size": 3000, "strike_step": 5,   "base": 370},
    "ASIANPAINT": {"lot_size": 200,  "strike_step": 20,  "base": 2800},
    "LTIM":       {"lot_size": 150,  "strike_step": 20,  "base": 5000},
    "ONGC":       {"lot_size": 1975, "strike_step": 5,   "base": 290},
    "POWERGRID":  {"lot_size": 2700, "strike_step": 5,   "base": 320},
    "TITAN":      {"lot_size": 375,  "strike_step": 20,  "base": 3300},
    # ── Nifty 100 additions ──────────────────────────────────────────────────
    "ITC":        {"lot_size": 1600, "strike_step": 5,   "base": 430},
    "LT":         {"lot_size": 175,  "strike_step": 20,  "base": 3300},
    "HCLTECH":    {"lot_size": 700,  "strike_step": 20,  "base": 1650},
    "M&M":        {"lot_size": 700,  "strike_step": 10,  "base": 2800},
    "COALINDIA":  {"lot_size": 1000, "strike_step": 5,   "base": 440},
    "HEROMOTOCO": {"lot_size": 300,  "strike_step": 50,  "base": 4000},
    "HINDALCO":   {"lot_size": 1075, "strike_step": 10,  "base": 650},
    "JSWSTEEL":   {"lot_size": 600,  "strike_step": 10,  "base": 940},
    "ULTRACEMCO": {"lot_size": 100,  "strike_step": 50,  "base": 10000},
    "GRASIM":     {"lot_size": 475,  "strike_step": 20,  "base": 2700},
    "BPCL":       {"lot_size": 1800, "strike_step": 5,   "base": 310},
    "CIPLA":      {"lot_size": 650,  "strike_step": 10,  "base": 1550},
    "DRREDDY":    {"lot_size": 250,  "strike_step": 50,  "base": 5500},
    "EICHERMOT":  {"lot_size": 200,  "strike_step": 50,  "base": 5000},
    "ADANIENT":   {"lot_size": 250,  "strike_step": 20,  "base": 2200},
    "ADANIPORTS": {"lot_size": 625,  "strike_step": 20,  "base": 1300},
    "APOLLOHOSP": {"lot_size": 125,  "strike_step": 50,  "base": 6500},
    "TATACONSUM": {"lot_size": 867,  "strike_step": 10,  "base": 1050},
    "TRENT":      {"lot_size": 350,  "strike_step": 50,  "base": 4800},
    "BAJAJFINSV": {"lot_size": 500,  "strike_step": 20,  "base": 1800},
    "DIVISLAB":   {"lot_size": 375,  "strike_step": 50,  "base": 4500},
    "SHRIRAMFIN": {"lot_size": 500,  "strike_step": 10,  "base": 700},
}


def _next_weekday(anchor: date, target_weekday: int) -> date:
    """Return the next occurrence of target_weekday (Mon=0 … Fri=4) after anchor."""
    days_ahead = (target_weekday - anchor.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return anchor + timedelta(days=days_ahead)


def _next_thursday(anchor: date) -> date:
    return _next_weekday(anchor, 3)


def _next_friday(anchor: date) -> date:
    return _next_weekday(anchor, 4)


def _next_monday(anchor: date) -> date:
    return _next_weekday(anchor, 0)


def _last_weekday_of_month(anchor: date, target_weekday: int) -> date:
    """Return the last occurrence of target_weekday in anchor's next month."""
    first_next_month = date(anchor.year + (anchor.month == 12), 1 if anchor.month == 12 else anchor.month + 1, 1)
    last_day = first_next_month - timedelta(days=1)
    offset = (last_day.weekday() - target_weekday) % 7
    return last_day - timedelta(days=offset)


def _monthly_expiry(anchor: date) -> date:
    """Last Thursday of the month (NSE standard)."""
    return _last_weekday_of_month(anchor, 3)


def _monthly_friday_expiry(anchor: date) -> date:
    """Last Friday of the month (BSE SENSEX standard)."""
    return _last_weekday_of_month(anchor, 4)


def _monthly_monday_expiry(anchor: date) -> date:
    """Last Monday of the month (BSE BANKEX standard)."""
    return _last_weekday_of_month(anchor, 0)


def _add_months(anchor: date, months: int) -> date:
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, 28)
    return date(year, month, day)


@dataclass
class InstrumentMaster:
    instruments: dict[str, Instrument]

    def get(self, symbol: str) -> Instrument:
        try:
            return self.instruments[symbol]
        except KeyError as exc:
            raise KeyError(f"Unknown instrument symbol: {symbol}") from exc

    def all(self) -> list[Instrument]:
        return list(self.instruments.values())

    def by_underlying(self, underlying: str, segment: Segment | None = None) -> list[Instrument]:
        return [
            instrument
            for instrument in self.instruments.values()
            if instrument.underlying == underlying and (segment is None or instrument.segment == segment)
        ]

    def expiries(self, underlying: str) -> list[date]:
        return sorted(
            {
                instrument.expiry
                for instrument in self.by_underlying(underlying)
                if instrument.expiry is not None
            }
        )

    def nearest_expiry(self, underlying: str, as_of: date) -> date:
        future_expiries = [expiry for expiry in self.expiries(underlying) if expiry >= as_of]
        if not future_expiries:
            raise ValueError(f"No future expiry found for {underlying} from {as_of}")
        return future_expiries[0]

    def select_future(self, underlying: str, as_of: date) -> Instrument:
        expiry = self.nearest_expiry(underlying, as_of)
        futures = [
            instrument
            for instrument in self.by_underlying(underlying, Segment.FUTURES)
            if instrument.expiry == expiry
        ]
        if not futures:
            raise ValueError(f"No future contract found for {underlying} {expiry}")
        return futures[0]

    def select_option(
        self,
        underlying: str,
        as_of: date,
        spot_price: float,
        option_type: OptionType,
        moneyness_steps: int = 0,
    ) -> Instrument:
        expiry = self.nearest_expiry(underlying, as_of)
        options = [
            instrument
            for instrument in self.by_underlying(underlying, Segment.OPTIONS)
            if instrument.expiry == expiry and instrument.option_type == option_type
        ]
        if not options:
            raise ValueError(f"No option contracts found for {underlying} {expiry} {option_type.value}")
        step = INDEX_UNDERLYINGS.get(underlying, {"strike_step": 50})["strike_step"]
        atm = round(spot_price / step) * step
        strike = atm + (moneyness_steps * step if option_type == OptionType.CE else -moneyness_steps * step)
        return min(options, key=lambda instrument: abs((instrument.strike or 0) - strike))


def _add_equity(universe: dict[str, Instrument], symbol: str, token: str) -> None:
    universe[symbol] = Instrument(
        symbol=symbol,
        name=symbol,
        exchange=Exchange.NSE,
        segment=Segment.CASH,
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.EQUITY,
        token=token,
    )


def _add_index(universe: dict[str, Instrument], symbol: str, token: str, exchange: Exchange = Exchange.NSE) -> None:
    universe[symbol] = Instrument(
        symbol=symbol,
        name=symbol,
        exchange=exchange,
        segment=Segment.CASH,
        asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.INDEX,
        token=token,
        underlying=symbol,
    )


def _add_derivatives(universe: dict[str, Instrument], as_of: date, underlyings: Iterable[str]) -> None:
    for underlying in underlyings:
        is_index = underlying in INDEX_UNDERLYINGS
        meta = INDEX_UNDERLYINGS[underlying] if is_index else EQUITY_FO_UNDERLYINGS[underlying]
        is_bse = meta.get("exchange") == "BSE"
        expiry_day = meta.get("expiry_day", "thursday")  # "thursday" | "friday" | "monday"

        # Determine exchange for derivatives
        fo_exchange = Exchange.BFO if is_bse else Exchange.NFO

        # Compute expiry set
        if is_index:
            if expiry_day == "friday":
                weekly_anchor = _next_friday(as_of)
                monthly_fn = _monthly_friday_expiry
            elif expiry_day == "monday":
                weekly_anchor = _next_monday(as_of)
                monthly_fn = _monthly_monday_expiry
            else:
                weekly_anchor = _next_thursday(as_of)
                monthly_fn = _monthly_expiry
            weekly_expiries = {weekly_anchor + timedelta(days=7 * i) for i in range(9)}
            monthly_expiries = {monthly_fn(_add_months(as_of, i)) for i in range(3)}
            expiries = sorted(weekly_expiries | monthly_expiries)
        else:
            expiries = sorted({_monthly_expiry(_add_months(as_of, i)) for i in range(3)})

        base = meta.get("base", 1000)
        step = meta["strike_step"]
        asset_class = AssetClass.INDEX if is_index else AssetClass.EQUITY

        for expiry in expiries:
            future_symbol = f"{underlying}{expiry:%d%b%y}FUT".upper()
            universe[future_symbol] = Instrument(
                symbol=future_symbol,
                name=f"{underlying} Future {expiry.isoformat()}",
                exchange=fo_exchange,
                segment=Segment.FUTURES,
                asset_class=asset_class,
                instrument_type=InstrumentType.FUTURE,
                token=f"FUT-{underlying}-{expiry:%Y%m%d}",
                lot_size=meta["lot_size"],
                expiry=expiry,
                underlying=underlying,
            )
            for offset in range(-5, 6):
                strike = base + offset * step
                for option_type in (OptionType.CE, OptionType.PE):
                    option_symbol = f"{underlying}{expiry:%d%b%y}{int(strike)}{option_type.value}".upper()
                    universe[option_symbol] = Instrument(
                        symbol=option_symbol,
                        name=f"{underlying} {strike:g} {option_type.value} {expiry.isoformat()}",
                        exchange=fo_exchange,
                        segment=Segment.OPTIONS,
                        asset_class=asset_class,
                        instrument_type=InstrumentType.OPTION,
                        token=f"OPT-{underlying}-{expiry:%Y%m%d}-{int(strike)}-{option_type.value}",
                        lot_size=meta["lot_size"],
                        expiry=expiry,
                        strike=float(strike),
                        option_type=option_type,
                        underlying=underlying,
                    )


def build_default_universe(as_of: date | None = None) -> InstrumentMaster:
    anchor = as_of or date.today()
    universe: dict[str, Instrument] = {}
    for symbol, meta in INDEX_UNDERLYINGS.items():
        exchange = Exchange.BSE if meta.get("exchange") == "BSE" else Exchange.NSE
        _add_index(universe, symbol, meta["token"], exchange)
    for symbol, token in LIQUID_EQUITIES.items():
        _add_equity(universe, symbol, token)
    # Build F&O derivatives for all indices (NSE → NFO, BSE → BFO) and equity F&O underlyings
    _add_derivatives(universe, anchor, [*INDEX_UNDERLYINGS.keys(), *EQUITY_FO_UNDERLYINGS.keys()])
    return InstrumentMaster(universe)
