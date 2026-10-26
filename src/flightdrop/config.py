from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .airports import REGIONS
from .models import CABINS, Cabin


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RelativeRule:
    window_days: int
    min_samples: int
    max_ratio: float


@dataclass(frozen=True)
class VerifyConfig:
    enabled: bool
    daily_budget: int
    monthly_budget: int
    max_live_over_cached: float
    # True: a fare that can't be checked against Google's live price is held, never posted unverified.
    require_live: bool = False


@dataclass(frozen=True)
class RssConfig:
    enabled: bool
    keywords: tuple[str, ...]
    feeds: dict[str, str]
    max_age_hours: int = 48


@dataclass(frozen=True)
class DestinationFilter:
    """Empty include lists mean "anywhere"; include_regions and include_countries are unioned."""

    include_regions: tuple[str, ...] = ()
    include_countries: tuple[str, ...] = ()
    exclude_countries: tuple[str, ...] = ()
    exclude_airports: tuple[str, ...] = ()


@dataclass(frozen=True)
class TypicalRule:
    """Deal = live Google price meaningfully below the route's usual (average) price."""

    enabled: bool = False
    below_usual_pct: float = 15.0
    accept_google_low: bool = True


@dataclass(frozen=True)
class AlertHours:
    """Daily window (local time in ``Config.timezone``) when alerts may be posted."""

    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end        # window wraps past midnight


@dataclass(frozen=True)
class AirlineFilter:
    """IATA 2-character carrier codes. Empty ``include`` means any airline; ``exclude`` always wins."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # Not a filter: alerts on these carriers also show the cheapest fare on any other airline.
    alternatives_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class TripWindow:
    min_days: int = 0
    max_days: int | None = None
    depart_min_days: int = 0
    depart_max_days: int | None = None
    months: tuple[int, ...] = ()


@dataclass(frozen=True)
class WatchItem:
    origins: tuple[str, ...]
    destination: str
    cabin: Cabin
    # Fixed target (or, with ``lower_than_previous``, an optional ceiling). None = no ceiling.
    max_price_usd: float | None = None
    # Moving target: alert on the first fare seen, then only on fares lower than the last alerted one.
    lower_than_previous: bool = False


@dataclass(frozen=True)
class Config:
    home_airports: tuple[str, ...]
    cabins: tuple[Cabin, ...]
    max_alerts_per_day: int
    timezone: str
    relative: RelativeRule
    realert_min_drop: float
    verify: VerifyConfig
    thresholds_usd: dict[str, dict[str, float]]
    rss: RssConfig
    destinations: DestinationFilter = field(default_factory=DestinationFilter)
    trip: TripWindow = field(default_factory=TripWindow)
    watchlist: tuple[WatchItem, ...] = ()
    airlines: AirlineFilter = field(default_factory=AirlineFilter)
    # ISO-2 destination country -> {cabin: round-trip USD}. Overrides the region's price.
    thresholds_by_country: dict[str, dict[str, float]] = field(default_factory=dict)
    alert_hours: AlertHours | None = None            # None = alert at any time
    typical: TypicalRule = field(default_factory=TypicalRule)


def _require(d: dict[str, Any], key: str) -> Any:
    if key not in d:
        raise ConfigError(f"missing config key: {key}")
    return d[key]


def _codes(values: Any, length: int, what: str) -> tuple[str, ...]:
    out = tuple(str(v).strip().upper() for v in (values or []))
    bad = [v for v in out if len(v) != length or not v.isalpha()]
    if bad:
        raise ConfigError(f"{what} must be {length}-letter codes; got {bad}")
    return out


def _parse_typical(raw: Any) -> TypicalRule:
    if raw is None:
        return TypicalRule()
    if not isinstance(raw, dict):
        raise ConfigError("typical must be a mapping like {enabled: true, below_usual_pct: 15}")
    try:
        pct = float(raw.get("below_usual_pct", 15))
    except (TypeError, ValueError):
        raise ConfigError("typical.below_usual_pct must be a number") from None
    if not 0 < pct < 90:
        raise ConfigError("typical.below_usual_pct must be between 0 and 90")
    return TypicalRule(bool(raw.get("enabled", False)), pct, bool(raw.get("accept_google_low", True)))


def _parse_thresholds(raw: Any, cabins: tuple[Cabin, ...]) -> dict[str, dict[str, float]]:
    """Optional regional price bars: region -> {cabin: round-trip USD}. Missing regions are fine."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("thresholds_usd must be a mapping of region -> prices")
    out: dict[str, dict[str, float]] = {}
    for region, prices in raw.items():
        if region not in REGIONS:
            raise ConfigError(f"thresholds_usd has unknown region {region!r}; valid: {list(REGIONS)}")
        if not isinstance(prices, dict):
            raise ConfigError(f"thresholds_usd.{region} must map cabins to prices")
        out[region] = {}
        for cabin, value in prices.items():
            if cabin not in CABINS:
                raise ConfigError(f"thresholds_usd.{region}: unknown cabin {cabin!r}")
            try:
                out[region][cabin] = float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"thresholds_usd.{region}.{cabin} must be a number") from None
    return out


def _clock(value: Any, what: str) -> time:
    # Unquoted 21:30 is read by YAML as the number 1290 (base-60), so a string is required.
    if not isinstance(value, str):
        raise ConfigError(f'{what} must be a quoted 24-hour time like "08:00"; got {value!r}')
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise ConfigError(f'{what} must look like "08:00" (24-hour HH:MM); got {value!r}') from None


def _parse_alert_hours(raw: Any) -> AlertHours | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or "start" not in raw or "end" not in raw:
        raise ConfigError('alert_hours needs both start and end, e.g. {start: "08:00", end: "21:30"}')
    window = AlertHours(_clock(raw["start"], "alert_hours.start"), _clock(raw["end"], "alert_hours.end"))
    if window.start == window.end:
        raise ConfigError("alert_hours.start and alert_hours.end must differ")
    return window


def _parse_country_thresholds(raw: Any) -> dict[str, dict[str, float]]:
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError("thresholds_by_country must be a mapping of country code -> prices")
    out: dict[str, dict[str, float]] = {}
    for code, prices in (raw or {}).items():
        (country,) = _codes([code], 2, "thresholds_by_country country")
        if not isinstance(prices, dict) or not prices:
            raise ConfigError(f"thresholds_by_country.{country} must give at least one cabin price")
        entry: dict[str, float] = {}
        for cabin, value in prices.items():
            if cabin not in CABINS:
                raise ConfigError(f"thresholds_by_country.{country}: unknown cabin {cabin!r}; use {list(CABINS)}")
            try:
                price = float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"thresholds_by_country.{country}.{cabin} must be a number") from None
            if price <= 0:
                raise ConfigError(f"thresholds_by_country.{country}.{cabin} must be positive")
            entry[cabin] = price
        out[country] = entry
    return out


def _airline_codes(values: Any, what: str) -> tuple[str, ...]:
    out = tuple(str(v).strip().upper() for v in (values or []))
    bad = [v for v in out if len(v) != 2 or not v.isalnum()]
    if bad:
        raise ConfigError(f"{what} must be 2-character airline codes like DL or F9; got {bad}")
    return out


def _parse_airlines(raw: dict[str, Any]) -> AirlineFilter:
    return AirlineFilter(
        include=_airline_codes(raw.get("include"), "airlines.include"),
        exclude=_airline_codes(raw.get("exclude"), "airlines.exclude"),
        alternatives_for=_airline_codes(raw.get("alternatives_for"), "airlines.alternatives_for"),
    )


def _parse_destinations(raw: dict[str, Any]) -> DestinationFilter:
    regions = tuple(str(r).strip().lower() for r in (raw.get("include_regions") or []))
    bad = [r for r in regions if r not in REGIONS]
    if bad:
        raise ConfigError(f"destinations.include_regions has unknown region(s) {bad}; valid: {list(REGIONS)}")
    return DestinationFilter(
        include_regions=regions,
        include_countries=_codes(raw.get("include_countries"), 2, "destinations.include_countries"),
        exclude_countries=_codes(raw.get("exclude_countries"), 2, "destinations.exclude_countries"),
        exclude_airports=_codes(raw.get("exclude_airports"), 3, "destinations.exclude_airports"),
    )


def _opt_int(value: Any, what: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a whole number or null; got {value!r}") from None


def _parse_trip(raw: dict[str, Any]) -> TripWindow:
    depart = raw.get("depart_in_days") or {}
    months = tuple(int(m) for m in (raw.get("depart_months") or []))
    if any(not 1 <= m <= 12 for m in months):
        raise ConfigError(f"trip.depart_months must be 1-12; got {list(months)}")
    trip = TripWindow(
        min_days=_opt_int(raw.get("min_days"), "trip.min_days") or 0,
        max_days=_opt_int(raw.get("max_days"), "trip.max_days"),
        depart_min_days=_opt_int(depart.get("min"), "trip.depart_in_days.min") or 0,
        depart_max_days=_opt_int(depart.get("max"), "trip.depart_in_days.max"),
        months=months,
    )
    if trip.max_days is not None and trip.min_days > trip.max_days:
        raise ConfigError("trip.min_days cannot be greater than trip.max_days")
    if trip.depart_max_days is not None and trip.depart_min_days > trip.depart_max_days:
        raise ConfigError("trip.depart_in_days.min cannot be greater than max")
    return trip


def _parse_watchlist(
    raw: list[dict[str, Any]], home: tuple[str, ...], cabins: tuple[Cabin, ...]
) -> tuple[WatchItem, ...]:
    items = []
    for i, entry in enumerate(raw or []):
        where = f"watchlist[{i}]"
        if not isinstance(entry, dict) or "to" not in entry:
            raise ConfigError(f"{where} needs 'to' and either 'max_price_usd' or alert_when: lower_than_previous")
        moving = entry.get("alert_when")
        if moving is not None and str(moving).strip().lower() != "lower_than_previous":
            raise ConfigError(f"{where}.alert_when must be 'lower_than_previous'; got {moving!r}")
        if "max_price_usd" not in entry and moving is None:
            raise ConfigError(f"{where} needs 'max_price_usd' or alert_when: lower_than_previous")
        (dest,) = _codes([entry["to"]], 3, f"{where}.to")
        origins = _codes([entry["from"]], 3, f"{where}.from") if entry.get("from") else home
        cabin = str(entry.get("cabin", "economy")).lower()
        if cabin not in cabins:
            raise ConfigError(f"{where}.cabin '{cabin}' is not enabled in 'cabins' {list(cabins)}")
        price = None
        if "max_price_usd" in entry:
            try:
                price = float(entry["max_price_usd"])
            except (TypeError, ValueError):
                raise ConfigError(f"{where}.max_price_usd must be a number") from None
            if price <= 0:
                raise ConfigError(f"{where}.max_price_usd must be positive")
        items.append(WatchItem(origins, dest, cabin, price, moving is not None))  # type: ignore[arg-type]
    return tuple(items)


def parse_config(raw: dict[str, Any]) -> Config:
    airports = _codes(_require(raw, "home_airports"), 3, "home_airports")
    if not airports:
        raise ConfigError("home_airports must list at least one airport")

    cabins = tuple(_require(raw, "cabins"))
    bad = [c for c in cabins if c not in CABINS]
    if not cabins or bad:
        raise ConfigError(f"cabins must be a non-empty subset of {CABINS}; got {cabins}")

    thresholds = _parse_thresholds(raw.get("thresholds_usd"), cabins)  # type: ignore[arg-type]

    rel = _require(raw, "relative")
    ver = _require(raw, "verify")
    rss = raw.get("rss") or {}

    cfg = Config(
        home_airports=airports,
        cabins=cabins,  # type: ignore[arg-type]
        max_alerts_per_day=int(_require(raw, "max_alerts_per_day")),
        timezone=str(raw.get("timezone", "America/Chicago")),
        relative=RelativeRule(
            window_days=int(rel["window_days"]),
            min_samples=int(rel["min_samples"]),
            max_ratio=float(rel["max_ratio"]),
        ),
        realert_min_drop=float(_require(raw, "realert_min_drop")),
        verify=VerifyConfig(
            enabled=bool(ver["enabled"]),
            daily_budget=int(ver["daily_budget"]),
            monthly_budget=int(ver["monthly_budget"]),
            max_live_over_cached=float(ver["max_live_over_cached"]),
            require_live=bool(ver.get("require_live", False)),
        ),
        thresholds_usd=thresholds,
        rss=RssConfig(
            enabled=bool(rss.get("enabled", False)),
            keywords=tuple(str(k) for k in rss.get("keywords", [])),
            feeds={str(k): str(v) for k, v in (rss.get("feeds") or {}).items()},
            max_age_hours=int(rss.get("max_age_hours", 48)),
        ),
        destinations=_parse_destinations(raw.get("destinations") or {}),
        trip=_parse_trip(raw.get("trip") or {}),
        watchlist=_parse_watchlist(raw.get("watchlist") or [], airports, cabins),  # type: ignore[arg-type]
        airlines=_parse_airlines(raw.get("airlines") or {}),
        thresholds_by_country=_parse_country_thresholds(raw.get("thresholds_by_country")),
        alert_hours=_parse_alert_hours(raw.get("alert_hours")),
        typical=_parse_typical(raw.get("typical")),
    )
    if cfg.typical.enabled and not cfg.verify.enabled:
        raise ConfigError("typical.enabled needs verify.enabled: the rule uses Google's live price history")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"timezone {cfg.timezone!r} is not a valid timezone name like America/Chicago") from None
    if cfg.max_alerts_per_day < 1:
        raise ConfigError("max_alerts_per_day must be >= 1")
    if not 0 < cfg.relative.max_ratio <= 1:
        raise ConfigError("relative.max_ratio must be in (0, 1]")
    return cfg


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a YAML mapping")
    return parse_config(raw)
