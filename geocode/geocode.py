"""
geocode.py
----------
Reads providers.db, geocodes each address via the Google Geocoding API,
and writes providers.geojson.

Usage:
    GOOGLE_API_KEY=AIza... python geocode.py
    python geocode.py --db providers.db --out providers.geojson --key AIza...

Behavior:
- Skips rows that already have lat/lng in the cache file (.geocode_cache.json)
  so re-runs are safe and cheap.
- Prints a summary of hits, misses, and errors at the end.
- Rows that could not be geocoded are written to the GeoJSON with
  geometry: null  (standard GeoJSON for "location unknown").
"""

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--db",  default="providers.db",       help="SQLite database")
    p.add_argument("--out", default="providers.geojson",  help="Output GeoJSON file")
    p.add_argument("--key", default=os.environ.get("GOOGLE_API_KEY", ""),
                   help="Google API key (or set GOOGLE_API_KEY env var)")
    p.add_argument("--delay", type=float, default=0.05,
                   help="Seconds between API calls (default 0.05 → ~20 req/s)")
    p.add_argument("--cache", default=".geocode_cache.json",
                   help="JSON file used to cache geocoding results")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode_address(address: str, api_key: str) -> tuple[float, float] | None:
    """
    Call the Google Geocoding API for a single address string.
    Returns (lat, lng) on success, None on miss or error.
    """
    import urllib.request
    import urllib.parse

    url = (
        "https://maps.googleapis.com/maps/api/geocode/json?"
        + urllib.parse.urlencode({"address": address, "key": api_key, "region": "de"})
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())

    if data["status"] == "OK" and data["results"]:
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]

    return None  # ZERO_RESULTS, REQUEST_DENIED, etc.


# ---------------------------------------------------------------------------
# Load DB
# ---------------------------------------------------------------------------

def load_providers(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GeoJSON builder
# ---------------------------------------------------------------------------

def build_feature(row: dict, latlon: tuple[float, float] | None) -> dict:
    languages = [l.strip() for l in row["languages"].split(",") if l.strip()] \
                if row["languages"] else []

    props = {
        "id":               row["id"],
        "praxis":           row["praxis"],
        "institution":      row["institution"],
        "doctor":           row["doctor"],
        "address":          row["address"],
        "phone":            row["phone"],
        "email":            row["email"],
        "website":          row["website"],
        "languages":        languages,
        "method_medicinal": row["method_medicinal"],  # 1, 0, or None
        "method_surgical":  row["method_surgical"],   # 1, 0, or None
    }

    geometry = (
        {"type": "Point", "coordinates": [latlon[1], latlon[0]]}  # GeoJSON: [lng, lat]
        if latlon else None
    )

    return {"type": "Feature", "geometry": geometry, "properties": props}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if not args.key:
        raise SystemExit(
            "No API key supplied. Pass --key AIza... or set GOOGLE_API_KEY."
        )

    # Load cache
    cache_path = Path(args.cache)
    cache: dict[str, list[float] | None] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    print(f"Cache loaded: {len(cache)} entries")

    providers = load_providers(args.db)
    print(f"Providers to geocode: {len(providers)}")

    hits = misses = errors = cached = 0
    features = []

    for i, row in enumerate(providers, 1):
        address = row["address"]

        # Cache hit
        if address in cache:
            latlon = tuple(cache[address]) if cache[address] else None
            cached += 1
        else:
            try:
                latlon = geocode_address(address, args.key)
                cache[address] = list(latlon) if latlon else None
                time.sleep(args.delay)
            except Exception as exc:
                print(f"  ERROR row {row['id']} ({address}): {exc}")
                latlon = None
                errors += 1
                cache[address] = None

            # Persist cache after every request
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

        if latlon:
            hits += 1
        else:
            misses += 1
            print(f"  MISS  row {row['id']}: {address}")

        features.append(build_feature(row, latlon))

        if i % 50 == 0:
            print(f"  {i}/{len(providers)} processed …")

    # Write GeoJSON
    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2))

    print(f"\nDone.")
    print(f"  Geocoded (API):  {hits + misses - cached} requests")
    print(f"  From cache:      {cached}")
    print(f"  Hits:            {hits}")
    print(f"  Misses (null):   {misses}")
    print(f"  Errors:          {errors}")
    print(f"  Output:          {out_path} ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()