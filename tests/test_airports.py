from __future__ import annotations

import pytest

from flightdrop.airports import city_name, lookup, region_for


@pytest.mark.parametrize(
    "iata,region",
    [
        ("MSP", "domestic"),
        ("HNL", "domestic"),
        ("SJU", "domestic"),      # Puerto Rico counts as domestic
        ("CUN", "nearby_intl"),   # Mexico
        ("YYZ", "nearby_intl"),   # Canada
        ("SJO", "nearby_intl"),   # Costa Rica
        ("LIS", "europe"),
        ("LHR", "europe"),
        ("NRT", "asia_oceania"),
        ("SYD", "asia_oceania"),
        ("DXB", "other_intl"),    # Middle East is its own bucket
        ("GRU", "other_intl"),    # South America
        ("JNB", "other_intl"),    # Africa
    ],
)
def test_region_for(iata, region):
    assert region_for(iata) == region


@pytest.mark.parametrize(
    "code,region,city",
    [
        ("NYC", "domestic", "New York"),      # metro codes show up as destinations in the fare data
        ("CHI", "domestic", "Chicago"),
        ("WAS", "domestic", "Washington"),
        ("LON", "europe", "London"),
        ("PAR", "europe", "Paris"),
        ("ROM", "europe", "Rome"),
        ("TYO", "asia_oceania", "Tokyo"),
        ("SEL", "asia_oceania", "Seoul"),
        ("YTO", "nearby_intl", "Toronto"),
        ("RIO", "other_intl", "Rio de Janeiro"),
        ("PBI", "domestic", "West Palm Beach"),   # missing from the OurAirports snapshot
    ],
)
def test_metro_codes_and_gap_fillers(code, region, city):
    assert region_for(code) == region and city_name(code) == city


def test_real_airports_are_not_overridden_by_the_extra_table():
    assert lookup("LHR").name.startswith("London Heathrow")


def test_unknown_code_has_no_region():
    assert region_for("ZZZ") is None
    assert lookup("ZZZ") is None


def test_lookup_is_case_insensitive_and_city_name_falls_back():
    assert lookup("lis").country == "PT"
    assert city_name("LIS") == "Lisbon"
    assert city_name("ZZZ") == "ZZZ"


def test_country_helpers():
    from flightdrop.airports import country_for, country_name
    assert country_for("ZRH") == "CH" and country_for("TYO") == "JP" and country_for("ZZZ") is None
    assert country_name("GT") == "Guatemala" and country_name("kg") == "Kyrgyzstan"
    assert country_name("XX") == "XX"                       # unlisted countries fall back to the code


def test_every_country_the_user_cares_about_has_airports_in_the_table():
    from flightdrop.airports import find_by_city, lookup
    for code in ("GUA", "ZRH", "GVA", "FCO", "MXP", "VCE", "DPS", "CGK", "NRT", "HND", "KIX", "SGN", "HAN", "FRU", "BSZ", "OSS", "ALA", "NQZ", "TSE", "ROM", "MIL", "TYO", "OSA"):
        assert lookup(code) is not None, code


@pytest.mark.parametrize("code,expected", [
    ("ROM", ("FCO", "CIA")), ("TYO", ("HND", "NRT")), ("MIL", ("MXP", "LIN", "BGY")), ("NYC", ("JFK", "LGA", "EWR")),
    ("LON", ("LHR", "LGW", "STN", "LTN", "LCY", "SEN")), ("FRU", ("BSZ",)), ("TSE", ("NQZ",)),
    ("ORL", ("MCO", "SFB")), ("DFW", ("DFW",)), ("zrh", ("ZRH",)),
])
def test_metro_codes_expand_to_airports_for_google(code, expected):
    from flightdrop.airports import airport_codes_for
    assert airport_codes_for(code) == expected


def test_every_metro_expansion_is_made_of_real_airport_codes():
    from flightdrop.airports import _METRO_AIRPORTS
    for metro, airports in _METRO_AIRPORTS.items():
        assert airports and all(len(a) == 3 and a.isalpha() and a.isupper() for a in airports), metro
