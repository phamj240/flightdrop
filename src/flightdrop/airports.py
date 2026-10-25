from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

Region = str  # one of REGIONS

REGIONS = ("domestic", "nearby_intl", "europe", "asia_oceania", "other_intl")

_DATA = Path(__file__).parent / "data" / "airports.csv"

# US territories are treated as domestic (no passport needed).
_DOMESTIC_COUNTRIES = {"US", "PR", "VI"}
_NEARBY_COUNTRIES = {
    "CA", "MX", "BS", "BB", "BZ", "CR", "CU", "DO", "SV", "GT", "HN", "HT", "JM",
    "NI", "PA", "KY", "AW", "CW", "SX", "TT", "AG", "DM", "GD", "KN", "LC", "VC",
    "TC", "VG", "AI", "BM", "MS", "MQ", "GP", "BL", "MF", "BQ",
}
_MIDDLE_EAST = {"AE", "SA", "QA", "KW", "BH", "OM", "IL", "JO", "LB", "IR", "IQ", "SY", "YE", "PS"}


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    continent: str


# IATA metropolitan-area codes (the fare data uses these as destinations; OurAirports has
# only individual airports) plus airports missing from the OurAirports snapshot.
# (code, name, city, ISO country, continent)
_EXTRA = [
    ("BJS", "Beijing (all airports)", "Beijing", "CN", "AS"),
    ("BUE", "Buenos Aires (all airports)", "Buenos Aires", "AR", "SA"),
    ("BUH", "Bucharest (all airports)", "Bucharest", "RO", "EU"),
    ("CHI", "Chicago (all airports)", "Chicago", "US", "NA"),
    ("DTT", "Detroit (all airports)", "Detroit", "US", "NA"),
    ("JKT", "Jakarta (all airports)", "Jakarta", "ID", "AS"),
    ("LON", "London (all airports)", "London", "GB", "EU"),
    ("MIL", "Milan (all airports)", "Milan", "IT", "EU"),
    ("MOW", "Moscow (all airports)", "Moscow", "RU", "EU"),
    ("NYC", "New York (all airports)", "New York", "US", "NA"),
    ("OSA", "Osaka (all airports)", "Osaka", "JP", "AS"),
    ("PAR", "Paris (all airports)", "Paris", "FR", "EU"),
    ("REK", "Reykjavik (all airports)", "Reykjavik", "IS", "EU"),
    ("RIO", "Rio de Janeiro (all airports)", "Rio de Janeiro", "BR", "SA"),
    ("ROM", "Rome (all airports)", "Rome", "IT", "EU"),
    ("SAO", "Sao Paulo (all airports)", "Sao Paulo", "BR", "SA"),
    ("SEL", "Seoul (all airports)", "Seoul", "KR", "AS"),
    ("SPK", "Sapporo (all airports)", "Sapporo", "JP", "AS"),
    ("STO", "Stockholm (all airports)", "Stockholm", "SE", "EU"),
    ("TCI", "Tenerife (all airports)", "Tenerife", "ES", "EU"),
    ("TYO", "Tokyo (all airports)", "Tokyo", "JP", "AS"),
    ("WAS", "Washington (all airports)", "Washington", "US", "NA"),
    ("YMQ", "Montreal (all airports)", "Montreal", "CA", "NA"),
    ("YTO", "Toronto (all airports)", "Toronto", "CA", "NA"),
    ("PBI", "Palm Beach International Airport", "West Palm Beach", "US", "NA"),
    # Former codes that fare feeds may still use (OurAirports lists only the current ones).
    ("FRU", "Manas International Airport (former code)", "Bishkek", "KG", "AS"),
    ("TSE", "Astana International Airport (former code)", "Astana", "KZ", "AS"),
]


@lru_cache(maxsize=1)
def _load() -> dict[str, Airport]:
    with open(_DATA, newline="", encoding="utf-8") as f:
        airports = {
            r["iata"]: Airport(r["iata"], r["name"], r["city"], r["country"], r["continent"])
            for r in csv.DictReader(f)
        }
    for code, name, city, country, continent in _EXTRA:
        airports.setdefault(code, Airport(code, name, city, country, continent))
    return airports


def lookup(iata: str) -> Airport | None:
    return _load().get(iata.upper())


def region_for(iata: str) -> Region | None:
    """Map an airport code to a pricing region, or None if the code is unknown."""
    ap = lookup(iata)
    if ap is None:
        return None
    if ap.country in _DOMESTIC_COUNTRIES:
        return "domestic"
    if ap.country in _NEARBY_COUNTRIES:
        return "nearby_intl"
    if ap.country in _MIDDLE_EAST:
        return "other_intl"
    if ap.continent == "EU":
        return "europe"
    if ap.continent in ("AS", "OC"):
        return "asia_oceania"
    return "other_intl"  # Africa, South America, Antarctica, remaining North America


def _norm_city(name: str) -> str:
    """'San José (Alajuela)' -> 'san jose'; accents and parentheticals removed."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\s*\(.*?\)", "", name).strip().lower()


@lru_cache(maxsize=1)
def _by_city() -> dict[str, list[Airport]]:
    index: dict[str, list[Airport]] = {}
    for ap in _load().values():
        if ap.city:
            index.setdefault(_norm_city(ap.city), []).append(ap)
    return index


def find_by_city(name: str) -> list[Airport]:
    """Airports whose city matches ``name`` (case/accent-insensitive), sorted by code."""
    return sorted(_by_city().get(_norm_city(name), []), key=lambda a: a.iata)


_COUNTRY_NAMES = {
    "US": "US", "CA": "Canada", "MX": "Mexico", "GT": "Guatemala", "CR": "Costa Rica", "PA": "Panama",
    "BZ": "Belize", "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua", "CU": "Cuba",
    "DO": "Dominican Republic", "JM": "Jamaica", "BS": "Bahamas", "CO": "Colombia", "PE": "Peru",
    "EC": "Ecuador", "BR": "Brazil", "AR": "Argentina", "CL": "Chile",
    "CH": "Switzerland", "IT": "Italy", "FR": "France", "DE": "Germany", "ES": "Spain", "PT": "Portugal",
    "GB": "UK", "IE": "Ireland", "IS": "Iceland", "NL": "Netherlands", "BE": "Belgium", "AT": "Austria",
    "GR": "Greece", "TR": "Turkey", "PL": "Poland", "CZ": "Czechia", "HU": "Hungary", "HR": "Croatia",
    "DK": "Denmark", "SE": "Sweden", "NO": "Norway", "FI": "Finland", "RS": "Serbia", "ME": "Montenegro",
    "AL": "Albania", "SI": "Slovenia", "MT": "Malta",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "TW": "Taiwan", "HK": "Hong Kong",
    "ID": "Indonesia", "VN": "Vietnam", "TH": "Thailand", "PH": "Philippines", "MY": "Malaysia",
    "SG": "Singapore", "KH": "Cambodia", "LA": "Laos", "IN": "India", "NP": "Nepal", "LK": "Sri Lanka",
    "KG": "Kyrgyzstan", "KZ": "Kazakhstan", "UZ": "Uzbekistan", "GE": "Georgia", "AM": "Armenia",
    "MN": "Mongolia", "AU": "Australia", "NZ": "New Zealand",
    "AE": "UAE", "QA": "Qatar", "JO": "Jordan", "EG": "Egypt", "MA": "Morocco", "KE": "Kenya",
    "ZA": "South Africa", "ET": "Ethiopia", "NG": "Nigeria",
}


def country_for(iata: str) -> str | None:
    """ISO-2 country of an airport or metro code, or None if unknown."""
    ap = lookup(iata)
    return ap.country if ap else None


def country_name(code: str) -> str:
    """'GT' -> 'Guatemala'. Unlisted countries are shown by their code."""
    return _COUNTRY_NAMES.get(code.upper(), code.upper())


def city_name(iata: str) -> str:
    ap = lookup(iata)
    return ap.city if ap and ap.city else iata
