#!/usr/bin/env python3
"""
macdive_gps_backfill.py — Extract GPS coordinates from Shearwater Swift AI
transmitter data stored in MacDive's database and backfill missing dive sites.

For each Shearwater dive that has raw DC memory (ZRAWDATA) but no GPS on its
dive site, this script:
  1. Decompresses the Shearwater native format (LRE + XOR)
  2. Checks for AI_ON_GPS mode (Swift AI with GPS)
  3. Extracts entry/exit GPS from opening/closing record 9
  4. Creates a new dive site with the entry GPS coordinates
  5. Reverse-geocodes the coordinates to get country and location name
  6. Appends entry and exit coordinates to the dive's notes

Requires: Python 3.6+, no external dependencies (stdlib only).
MacDive must be closed before running. Network access needed for geocoding.

Usage:
    python3 macdive_gps_backfill.py                  # dry-run (default)
    python3 macdive_gps_backfill.py --apply           # write changes
    python3 macdive_gps_backfill.py --no-geocode      # skip reverse geocoding
    python3 macdive_gps_backfill.py --db /path/to.db  # custom database path
"""

import argparse
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
import urllib.request
from pathlib import Path

# --- Shearwater decompression ------------------------------------------------

def decompress_lre(block: bytes) -> bytes:
    """Decode one 144-byte LRE-compressed block (9-bit packed values)."""
    out = bytearray()
    nbits = len(block) * 8
    offset = 0
    while offset + 9 <= nbits:
        byte_idx = offset // 8
        bit_idx = offset % 8
        if byte_idx + 1 >= len(block):
            break
        raw = (block[byte_idx] << 8) | block[byte_idx + 1]
        value = (raw >> (16 - bit_idx - 9)) & 0x1FF
        if value & 0x100:
            out.append(value & 0xFF)
        elif value == 0:
            break
        else:
            out.extend(b'\x00' * value)
        offset += 9
    return bytes(out)


def decompress_xor(data: bytes) -> bytes:
    """Apply XOR decompression across full result."""
    result = bytearray(data)
    for i in range(32, len(result)):
        result[i] ^= result[i - 32]
    return bytes(result)


def decompress_shearwater(raw: bytes) -> bytes:
    """Full decompression: LRE per 144-byte block, then XOR."""
    blocks = bytearray()
    for i in range(0, len(raw), 144):
        blocks.extend(decompress_lre(raw[i:i + 144]))
    return decompress_xor(bytes(blocks))


# --- GPS extraction -----------------------------------------------------------

AI_ON_GPS = 6

def extract_gps(raw_data: bytes):
    """
    Extract GPS from decompressed Shearwater native format.
    Returns (entry_lat, entry_lon, exit_lat, exit_lon) or None.
    Coordinates are decimal degrees; None for missing exit.
    """
    data = decompress_shearwater(raw_data)
    if len(data) < 64:
        return None

    aimode = None
    entry = None
    exit_ = None

    for i in range(0, len(data) - 31, 32):
        rtype = data[i]

        if rtype == 0x14:  # Opening Record 4 — AI mode
            if data[i + 16] >= 7:  # logversion >= 7
                aimode = data[i + 28]

        elif rtype == 0x19:  # Opening Record 9 — entry GPS
            lat = struct.unpack('>i', data[i + 21:i + 25])[0]
            lon = struct.unpack('>i', data[i + 25:i + 29])[0]
            if (lat, lon) not in ((0, 0), (-1, -1)):
                entry = (lat / 100000.0, lon / 100000.0)

        elif rtype == 0x29:  # Closing Record 9 — exit GPS
            lat = struct.unpack('>i', data[i + 21:i + 25])[0]
            lon = struct.unpack('>i', data[i + 25:i + 29])[0]
            if (lat, lon) not in ((0, 0), (-1, -1)):
                exit_ = (lat / 100000.0, lon / 100000.0)

    if aimode != AI_ON_GPS or entry is None:
        return None

    return entry, exit_


# --- Reverse geocoding --------------------------------------------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "macdive-gps-backfill/1.0 (dive log utility)"

def reverse_geocode(lat, lon):
    """
    Reverse-geocode coordinates via Nominatim (OpenStreetMap).
    Returns (country, location, water_body) or None on failure.
    """
    url = (f"{NOMINATIM_URL}?lat={lat}&lon={lon}"
           f"&format=json&zoom=10&addressdetails=1&accept-language=en")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"    geocoding failed: {e}")
        return None

    addr = data.get("address", {})
    country = addr.get("country", "")
    location = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("island") or addr.get("county")
                or addr.get("state") or "")
    water = (addr.get("water") or addr.get("bay")
             or addr.get("sea") or "")
    return country, location, water


# --- MacDive database operations ----------------------------------------------

DEFAULT_DB = os.path.expanduser(
    "~/Library/Application Support/MacDive/MacDive.sqlite"
)

def find_candidates(db):
    """Find Shearwater dives with ZRAWDATA but no GPS on their dive site."""
    return db.execute("""
        SELECT d.Z_PK, d.ZDIVENUMBER, d.ZRAWDATA, d.ZNOTES,
               d.ZRELATIONSHIPDIVESITE, d.Z_OPT
        FROM ZDIVE d
        LEFT JOIN ZDIVESITE s ON d.ZRELATIONSHIPDIVESITE = s.Z_PK
        WHERE d.ZRAWDATA IS NOT NULL
          AND d.ZPARSERTYPE LIKE 'shearwater%'
          AND (d.ZRELATIONSHIPDIVESITE IS NULL
               OR s.ZGPSLAT IS NULL OR s.ZGPSLAT = 0.0)
        ORDER BY d.ZDIVENUMBER
    """).fetchall()


def get_divesite_ent(db):
    """Get the Z_ENT value for the DiveSite entity."""
    row = db.execute(
        "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'DiveSite'"
    ).fetchone()
    if not row:
        print("ERROR: Z_PRIMARYKEY has no DiveSite entity. Is this a MacDive database?")
        sys.exit(1)
    return row[0]


def next_site_pk(db):
    """Get the next Z_PK for ZDIVESITE (current Z_MAX + 1)."""
    row = db.execute(
        "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'DiveSite'"
    ).fetchone()
    return row[0] + 1


def apply_gps(db, dive_pk, dive_num, dive_z_opt, dive_notes,
              entry, exit_, site_ent, geo, dry_run):
    """Create a dive site and update the dive record."""
    entry_lat, entry_lon = entry
    gps_text = f"Entry: {entry_lat:.5f}, {entry_lon:.5f}"
    if exit_:
        gps_text += f" / Exit: {exit_[0]:.5f}, {exit_[1]:.5f}"

    geo_text = ""
    if geo:
        country, location, water = geo
        parts = [p for p in (location, country) if p]
        geo_text = ", ".join(parts)
        if geo_text:
            gps_text = f"{geo_text} — {gps_text}"

    # Build notes
    if dive_notes:
        new_notes = f"{dive_notes}\n\n[Swift AI GPS] {gps_text}"
    else:
        new_notes = f"[Swift AI GPS] {gps_text}"

    if dry_run:
        return gps_text

    # Create new ZDIVESITE
    site_pk = next_site_pk(db)
    now = time.time() - 978307200  # Core Data epoch

    db.execute("""
        INSERT INTO ZDIVESITE
            (Z_PK, Z_ENT, Z_OPT, ZGPSLAT, ZGPSLON,
             ZCOUNTRY, ZLOCATION, ZBODYOFWATER, ZMODIFIED)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
    """, (site_pk, site_ent, entry_lat, entry_lon,
          (geo[0] if geo else None),
          (geo[1] if geo else None),
          (geo[2] if geo else None),
          now))

    # Update Z_PRIMARYKEY.Z_MAX for DiveSite
    db.execute(
        "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = 'DiveSite'",
        (site_pk,)
    )

    # Link dive to new site and update notes
    db.execute("""
        UPDATE ZDIVE
        SET ZRELATIONSHIPDIVESITE = ?, ZNOTES = ?, Z_OPT = ?
        WHERE Z_PK = ?
    """, (site_pk, new_notes, dive_z_opt + 1, dive_pk))

    return gps_text


# --- Main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill GPS from Shearwater Swift AI into MacDive dive sites."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write changes to the database (default is dry-run)."
    )
    parser.add_argument(
        "--no-geocode", action="store_true",
        help="Skip reverse geocoding (no network access needed)."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"Path to MacDive.sqlite (default: {DEFAULT_DB})"
    )
    args = parser.parse_args()
    dry_run = not args.apply
    do_geocode = not args.no_geocode

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    if dry_run:
        print("=== DRY RUN (use --apply to write changes) ===\n")
    else:
        # Back up before modifying
        backup = db_path + ".bak"
        shutil.copy2(db_path, backup)
        print(f"Backup saved to: {backup}\n")

    db = sqlite3.connect(db_path)
    site_ent = get_divesite_ent(db)
    candidates = find_candidates(db)

    if not candidates:
        print("No Shearwater dives found with missing GPS.")
        db.close()
        return

    print(f"Found {len(candidates)} Shearwater dive(s) without GPS:\n")

    updated = 0
    skipped = 0

    for dive_pk, dive_num, raw_data, notes, site_fk, z_opt in candidates:
        label = f"Dive {dive_num}" if dive_num else f"Dive (PK={dive_pk})"

        result = extract_gps(raw_data)
        if result is None:
            print(f"  {label}: no Swift AI GPS in raw data — skipped")
            skipped += 1
            continue

        entry, exit_ = result

        # Reverse geocode entry coordinates
        geo = None
        if do_geocode:
            if updated > 0:
                time.sleep(1)  # Nominatim rate limit: 1 req/sec
            geo = reverse_geocode(entry[0], entry[1])

        gps_text = apply_gps(db, dive_pk, dive_num, z_opt, notes,
                             entry, exit_, site_ent, geo, dry_run)
        action = "would update" if dry_run else "updated"
        print(f"  {label}: {action} — {gps_text}")
        updated += 1

    if not dry_run:
        db.commit()

    db.close()

    print(f"\nDone. {updated} dive(s) {'would be ' if dry_run else ''}updated, "
          f"{skipped} skipped (no GPS).")

    if dry_run and updated > 0:
        print("\nRe-run with --apply to write changes.")


if __name__ == "__main__":
    main()
