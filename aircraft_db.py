from __future__ import annotations

import re

from models import AircraftProfile


KNOWN_PROFILES: dict[str, AircraftProfile] = {
    "A320": AircraftProfile("A320", 37.6, 34.1),
    "A321": AircraftProfile("A321", 44.5, 35.8),
    "A20N": AircraftProfile("A20N", 37.6, 35.8),
    "A319": AircraftProfile("A319", 33.8, 34.1),
    "A332": AircraftProfile("A332", 58.8, 60.3),
    "A359": AircraftProfile("A359", 66.8, 64.8),
    "AT76": AircraftProfile("AT76", 27.2, 28.7),
    "B38M": AircraftProfile("B38M", 39.5, 35.9),
    "B738": AircraftProfile("B738", 39.5, 35.8),
    "B739": AircraftProfile("B739", 42.1, 35.8),
    "B763": AircraftProfile("B763", 54.9, 47.6),
    "B788": AircraftProfile("B788", 56.7, 60.1),
    "B772": AircraftProfile("B772", 63.7, 60.9),
    "B77W": AircraftProfile("B77W", 73.9, 64.8),
    "B744": AircraftProfile("B744", 70.7, 64.4),
    "CRJ9": AircraftProfile("CRJ9", 36.4, 24.9),
    "CRJ2": AircraftProfile("CRJ2", 26.8, 21.2),
    "E175": AircraftProfile("E175", 31.7, 26.0),
    "E190": AircraftProfile("E190", 36.2, 28.7),
    "MD88": AircraftProfile("MD88", 45.1, 32.9),
}


CALLSIGN_PREFIX_TO_TYPE: dict[str, str] = {
    "DAL": "B738",
    "AAL": "B738",
    "UAL": "B739",
    "ARG": "B738",
    "KLM": "B738",
    "AFR": "A320",
    "DLH": "A321",
    "IBE": "A320",
    "AZU": "E190",
    "JBU": "A320",
    "ASA": "E175",
    "RPA": "E175",
    "JIA": "CRJ9",
    "AWI": "CRJ9",
    "KAL": "B772",
    "QTR": "B77W",
    "CPA": "A359",
    "LAN": "A320",
    "THY": "A321",
    "VOI": "A320",
    "SWA": "B38M",
    "FFT": "A320",
    "NKS": "A320",
}


CALLSIGN_PREFIX_TO_FLEET: dict[str, tuple[str, ...]] = {
    "DAL": ("A319", "A320", "A321", "B738", "B739", "B763", "B772", "A332", "A359", "MD88"),
    "AAL": ("A319", "A320", "A321", "B738", "B38M", "B788", "B772"),
    "UAL": ("A319", "A320", "A321", "B738", "B739", "B763", "B772", "B788"),
    "ARG": ("B738", "A320", "A332"),
    "ASA": ("B738", "B739", "E175"),
    "AZU": ("E190", "A320", "A321"),
    "JBU": ("A320", "A321", "A20N"),
    "SWA": ("B738", "B38M"),
}


ICAO24_PREFIX_TO_TYPE: dict[str, str] = {
    "a4": "B738",
    "a8": "CRJ2",
    "c0": "B772",
    "e8": "A320",
    "39": "A359",
    "40": "A321",
    "ab": "B38M",
    "ac": "B788",
    "c1": "B744",
}


FALLBACK_PROFILES: list[AircraftProfile] = [
    AircraftProfile("Light", 11.0, 11.0),
    AircraftProfile("Regional", 27.0, 24.0),
    AircraftProfile("Narrowbody", 40.0, 36.0),
    AircraftProfile("Widebody", 60.0, 60.0),
    AircraftProfile("Heavy", 73.0, 68.0),
]


def _stable_bucket(seed_text: str, bucket_count: int) -> int:
    return sum(ord(char) for char in seed_text) % bucket_count


def resolve_aircraft_profile(icao24: str, callsign: str) -> AircraftProfile:
    callsign_upper = (callsign or "").upper()
    icao24_lower = (icao24 or "").lower()

    explicit_type_match = re.search(r"(A319|A320|A321|A20N|A332|A359|AT76|B38M|B738|B739|B744|B763|B772|B77W|B788|CRJ2|CRJ9|E175|E190|MD88)", callsign_upper)
    if explicit_type_match is not None:
        return KNOWN_PROFILES[explicit_type_match.group(1)]

    for prefix, type_code in CALLSIGN_PREFIX_TO_TYPE.items():
        if callsign_upper.startswith(prefix):
            return KNOWN_PROFILES[type_code]

    for prefix, fleet in CALLSIGN_PREFIX_TO_FLEET.items():
        if callsign_upper.startswith(prefix):
            bucket = _stable_bucket(f"{callsign_upper}:{icao24_lower}", len(fleet))
            return KNOWN_PROFILES[fleet[bucket]]

    for prefix, type_code in ICAO24_PREFIX_TO_TYPE.items():
        if icao24_lower.startswith(prefix):
            return KNOWN_PROFILES[type_code]

    tail_seed = icao24_lower[-2:] if len(icao24_lower) >= 2 else callsign_upper or "fallback"
    try:
        bucket = int(tail_seed, 16) % len(FALLBACK_PROFILES)
    except ValueError:
        bucket = _stable_bucket(tail_seed, len(FALLBACK_PROFILES))
    return FALLBACK_PROFILES[bucket]