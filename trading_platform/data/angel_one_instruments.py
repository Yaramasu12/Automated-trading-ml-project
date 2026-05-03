from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from trading_platform.config import Settings
from trading_platform.data.instrument_master import INDEX_UNDERLYINGS, InstrumentMaster
from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
)
from trading_platform.domain.models import Instrument


INDEX_NAMES = set(INDEX_UNDERLYINGS)


@dataclass(frozen=True)
class InstrumentMasterRefreshResult:
    source: str
    cache_path: str
    raw_count: int
    parsed_count: int
    skipped_count: int


class AngelOneInstrumentMasterProvider:
    """Loads Angel One OpenAPI instrument master into the platform model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache_path = Path(settings.angel_one_instrument_cache_path)

    def refresh(self, timeout_seconds: int = 30) -> InstrumentMasterRefreshResult:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(self.settings.angel_one_instrument_master_url, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        rows = json.loads(payload)
        self.cache_path.write_text(json.dumps(rows), encoding="utf-8")
        parsed = self._parse_rows(rows)
        return InstrumentMasterRefreshResult(
            source=self.settings.angel_one_instrument_master_url,
            cache_path=str(self.cache_path),
            raw_count=len(rows),
            parsed_count=len(parsed.instruments),
            skipped_count=max(0, len(rows) - len(parsed.instruments)),
        )

    def load_cached(self) -> InstrumentMaster:
        if not self.cache_path.exists():
            raise FileNotFoundError(f"Angel One instrument cache not found: {self.cache_path}")
        rows = json.loads(self.cache_path.read_text(encoding="utf-8"))
        return self._parse_rows(rows)

    def parse_rows(self, rows: list[dict[str, Any]]) -> InstrumentMaster:
        return self._parse_rows(rows)

    def _parse_rows(self, rows: list[dict[str, Any]]) -> InstrumentMaster:
        instruments: dict[str, Instrument] = {}
        for row in rows:
            instrument = self._parse_row(row)
            if instrument is not None:
                instruments[instrument.symbol] = instrument
        return InstrumentMaster(instruments)

    def _parse_row(self, row: dict[str, Any]) -> Instrument | None:
        exchange = _parse_exchange(row.get("exch_seg"))
        if exchange is None:
            return None

        symbol = str(row.get("symbol") or "").strip()
        token = str(row.get("token") or "").strip()
        name = str(row.get("name") or symbol).strip()
        instrument_type_raw = str(row.get("instrumenttype") or "").strip().upper()
        if not symbol or not token:
            return None

        segment, instrument_type = _classify(symbol, instrument_type_raw, exchange)
        if segment is None or instrument_type is None:
            return None

        option_type = _parse_option_type(symbol)
        expiry = _parse_expiry(row.get("expiry"))
        strike = _parse_strike(row.get("strike"))
        lot_size = _parse_int(row.get("lotsize"), default=1)
        tick_size = _parse_tick_size(row.get("tick_size"))
        underlying = _infer_underlying(name, symbol, instrument_type)
        asset_class = AssetClass.INDEX if _is_index_contract(name, symbol, instrument_type_raw) else AssetClass.EQUITY

        return Instrument(
            symbol=symbol,
            name=name,
            exchange=exchange,
            segment=segment,
            asset_class=asset_class,
            instrument_type=instrument_type,
            token=token,
            lot_size=max(1, lot_size),
            tick_size=tick_size,
            expiry=expiry,
            strike=strike if segment == Segment.OPTIONS else None,
            option_type=option_type if segment == Segment.OPTIONS else None,
            underlying=underlying,
        )


def _parse_exchange(value: Any) -> Exchange | None:
    if not value:
        return None
    raw = str(value).strip().upper()
    try:
        return Exchange(raw)
    except ValueError:
        return None


def _classify(symbol: str, instrument_type_raw: str, exchange: Exchange) -> tuple[Segment | None, InstrumentType | None]:
    if instrument_type_raw.startswith("FUT"):
        return Segment.FUTURES, InstrumentType.FUTURE
    if instrument_type_raw.startswith("OPT") or symbol.endswith(("CE", "PE")):
        return Segment.OPTIONS, InstrumentType.OPTION
    if exchange in {Exchange.NSE, Exchange.BSE}:
        if symbol.endswith("-EQ") or instrument_type_raw in {"", "EQ"}:
            return Segment.CASH, InstrumentType.EQUITY
        if instrument_type_raw in {"AMXIDX", "IDX"} or symbol in INDEX_NAMES:
            return Segment.CASH, InstrumentType.INDEX
    return None, None


def _parse_option_type(symbol: str) -> OptionType | None:
    if symbol.endswith("CE"):
        return OptionType.CE
    if symbol.endswith("PE"):
        return OptionType.PE
    return None


def _parse_expiry(value: Any) -> date | None:
    if value in {None, "", "NA"}:
        return None
    raw = str(value).strip().upper()
    for fmt in ("%d%b%Y", "%d%b%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_strike(value: Any) -> float | None:
    if value in {None, "", "NA"}:
        return None
    try:
        strike = float(value)
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    return strike / 100 if strike >= 100_000 else strike


def _parse_tick_size(value: Any) -> float:
    try:
        tick_size = float(value)
    except (TypeError, ValueError):
        return 0.05
    if tick_size >= 1:
        tick_size = tick_size / 100
    return tick_size or 0.05


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _infer_underlying(name: str, symbol: str, instrument_type: InstrumentType) -> str | None:
    if instrument_type in {InstrumentType.FUTURE, InstrumentType.OPTION}:
        return name.strip().upper() or None
    if instrument_type == InstrumentType.INDEX:
        return symbol.strip().upper()
    return None


def _is_index_contract(name: str, symbol: str, instrument_type_raw: str) -> bool:
    raw_name = name.strip().upper()
    raw_symbol = symbol.strip().upper()
    return instrument_type_raw.endswith("IDX") or raw_name in INDEX_NAMES or any(raw_symbol.startswith(index) for index in INDEX_NAMES)

