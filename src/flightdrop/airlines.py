from __future__ import annotations

# IATA 2-character airline code -> name, for display only. Filtering uses the code itself.
_NAMES = {
    # United States
    "AA": "American", "DL": "Delta", "UA": "United", "WN": "Southwest", "B6": "JetBlue",
    "AS": "Alaska", "NK": "Spirit", "F9": "Frontier", "SY": "Sun Country", "G4": "Allegiant",
    "HA": "Hawaiian", "XP": "Avelo", "MX": "Breeze",
    # Canada / Latin America
    "AC": "Air Canada", "WS": "WestJet", "AM": "Aeromexico", "Y4": "Volaris", "VB": "Viva Aerobus",
    "LA": "LATAM", "AV": "Avianca", "CM": "Copa", "AR": "Aerolineas Argentinas", "G3": "Gol",
    "AD": "Azul", "JA": "JetSMART",
    # Europe
    "BA": "British Airways", "VS": "Virgin Atlantic", "AF": "Air France", "KL": "KLM",
    "LH": "Lufthansa", "LX": "Swiss", "OS": "Austrian", "SN": "Brussels Airlines", "IB": "Iberia",
    "TP": "TAP Air Portugal", "AZ": "ITA Airways", "EI": "Aer Lingus", "FI": "Icelandair",
    "SK": "SAS", "AY": "Finnair", "LO": "LOT Polish", "TK": "Turkish", "OK": "Czech Airlines",
    "SU": "Aeroflot", "PS": "Ukraine International", "KM": "Air Malta", "JU": "Air Serbia",
    "LG": "Luxair", "VY": "Vueling", "FR": "Ryanair", "U2": "easyJet", "W6": "Wizz Air",
    # Middle East / Africa
    "QR": "Qatar Airways", "EK": "Emirates", "EY": "Etihad", "SV": "Saudia", "RJ": "Royal Jordanian",
    "MS": "EgyptAir", "ET": "Ethiopian", "KQ": "Kenya Airways", "SA": "South African",
    "AT": "Royal Air Maroc", "WY": "Oman Air", "GF": "Gulf Air", "KU": "Kuwait Airways",
    "LY": "El Al", "ME": "Middle East Airlines", "TU": "Tunisair", "AH": "Air Algerie",
    # Asia / Pacific
    "JL": "Japan Airlines", "NH": "ANA", "KE": "Korean Air", "OZ": "Asiana", "CX": "Cathay Pacific",
    "SQ": "Singapore Airlines", "TG": "Thai Airways", "BR": "EVA Air", "CI": "China Airlines",
    "CA": "Air China", "MU": "China Eastern", "CZ": "China Southern", "HU": "Hainan Airlines",
    "AI": "Air India", "UL": "SriLankan", "PR": "Philippine Airlines", "VN": "Vietnam Airlines",
    "MH": "Malaysia Airlines", "GA": "Garuda Indonesia", "5J": "Cebu Pacific", "PK": "PIA",
    "QF": "Qantas", "NZ": "Air New Zealand", "FJ": "Fiji Airways", "TN": "Air Tahiti Nui",
    "HX": "Hong Kong Airlines", "BG": "Biman Bangladesh",
}


def airline_name(code: str | None) -> str | None:
    """'F9' -> 'Frontier (F9)'. Unknown codes are shown as-is; None stays None."""
    if not code:
        return None
    name = _NAMES.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()
