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
    "NIFTY": {"token": "26000", "lot_size": 50, "strike_step": 50},
    "BANKNIFTY": {"token": "26009", "lot_size": 15, "strike_step": 100},
    "FINNIFTY": {"token": "26037", "lot_size": 40, "strike_step": 50},
    "MIDCPNIFTY": {"token": "26074", "lot_size": 75, "strike_step": 25},
}

LIQUID_EQUITIES = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "SBIN": "3045",
}

EQUITY_FO_UNDERLYINGS = {
    "RELIANCE": {"lot_size": 250, "strike_step": 20, "base": 2800},
    "TCS": {"lot_size": 175, "strike_step": 50, "base": 3500},
    "INFY": {"lot_size": 400, "strike_step": 20, "base": 1500},
    "HDFCBANK": {"lot_size": 550, "strike_step": 20, "base": 1600},
    "ICICIBANK": {"lot_size": 700, "strike_step": 10, "base": 1100},
    "SBIN": {"lot_size": 750, "strike_step": 10, "base": 750},
}


def _next_thursday(anchor: date) -> date:
    days_ahead = (3 - anchor.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return anchor + timedelta(days=days_ahead)


def _monthly_expiry(anchor: date) -> date:
    first_next_month = date(anchor.year + (anchor.month == 12), 1 if anchor.month == 12 else anchor.month + 1, 1)
    last_day = first_next_month - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7
    return last_day - timedelta(days=offset)


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


def _add_index(universe: dict[str, Instrument], symbol: str, token: str) -> None:
    universe[symbol] = Instrument(
        symbol=symbol,
        name=symbol,
        exchange=Exchange.NSE,
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
        weekly = _next_thursday(as_of)
        monthly_expiries = {_monthly_expiry(_add_months(as_of, offset)) for offset in range(3)}
        weekly_expiries = {weekly + timedelta(days=7 * offset) for offset in range(9)}
        expiries = sorted((weekly_expiries | monthly_expiries) if is_index else monthly_expiries)
        for expiry in expiries:
            future_symbol = f"{underlying}{expiry:%d%b%y}FUT".upper()
            universe[future_symbol] = Instrument(
                symbol=future_symbol,
                name=f"{underlying} Future {expiry.isoformat()}",
                exchange=Exchange.NFO,
                segment=Segment.FUTURES,
                asset_class=AssetClass.INDEX if is_index else AssetClass.EQUITY,
                instrument_type=InstrumentType.FUTURE,
                token=f"FUT-{underlying}-{expiry:%Y%m%d}",
                lot_size=meta["lot_size"],
                expiry=expiry,
                underlying=underlying,
            )
            index_bases = {
                "NIFTY": 22500,
                "BANKNIFTY": 48500,
                "FINNIFTY": 21500,
                "MIDCPNIFTY": 11800,
            }
            base = index_bases[underlying] if is_index else meta["base"]
            step = meta["strike_step"]
            for offset in range(-5, 6):
                strike = base + offset * step
                for option_type in (OptionType.CE, OptionType.PE):
                    option_symbol = f"{underlying}{expiry:%d%b%y}{int(strike)}{option_type.value}".upper()
                    universe[option_symbol] = Instrument(
                        symbol=option_symbol,
                        name=f"{underlying} {strike:g} {option_type.value} {expiry.isoformat()}",
                        exchange=Exchange.NFO,
                        segment=Segment.OPTIONS,
                        asset_class=AssetClass.INDEX if is_index else AssetClass.EQUITY,
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
    for symbol, token in INDEX_UNDERLYINGS.items():
        _add_index(universe, symbol, token)
    for symbol, token in LIQUID_EQUITIES.items():
        _add_equity(universe, symbol, token)
    _add_derivatives(universe, anchor, [*INDEX_UNDERLYINGS.keys(), *EQUITY_FO_UNDERLYINGS.keys()])
    return InstrumentMaster(universe)
