from __future__ import annotations

import pytest

from flightdrop.airlines import _NAMES, airline_name


def test_known_airlines_show_name_and_code():
    assert airline_name("F9") == "Frontier (F9)"
    assert airline_name("dl") == "Delta (DL)"           # case-insensitive
    assert airline_name("SY") == "Sun Country (SY)"


def test_unknown_airlines_fall_back_to_the_code():
    assert airline_name("ZZ") == "ZZ"
    assert airline_name("zz") == "ZZ"


def test_missing_airline_stays_missing():
    assert airline_name(None) is None
    assert airline_name("") is None


@pytest.mark.parametrize("code", sorted(_NAMES))
def test_every_table_entry_is_a_valid_two_character_code(code):
    assert len(code) == 2 and code.isalnum() and code == code.upper()
