# macdive-swiftai-gps

Backfill GPS coordinates from Shearwater Swift AI transmitter data into [MacDive](https://www.mac-dive.com/) dive sites.

If you dive with a Shearwater computer (Perdix 2, Petrel 3, Teric, etc.) paired with a [Swift AI GPS transmitter](https://www.shearwater.com/products/swift-gps/), your dive computer records entry and exit GPS coordinates in every dive log. MacDive imports the raw dive data but doesn't extract the embedded GPS — so your dives show up without map pins or location info.

This script fixes that. It reads the raw dive computer memory already stored in MacDive's database, decompresses it, extracts the GPS coordinates, creates dive sites with map pins, reverse-geocodes the country and region, and appends the coordinates to your dive notes.

## What It Does

For each Shearwater dive in your MacDive database that has raw data but no GPS:

1. **Decompresses** the Shearwater native format (LRE + XOR compression)
2. **Checks** for AI_ON_GPS mode — confirming a Swift AI GPS transmitter was active
3. **Extracts** entry (dive start) and exit (dive end) GPS coordinates
4. **Creates a dive site** with the entry GPS as the map pin location
5. **Reverse-geocodes** the coordinates to fill in country and region name
6. **Appends** entry and exit coordinates to the dive's notes field

Dives without a Swift AI GPS transmitter are skipped automatically.

## Requirements

- **macOS** (MacDive is Mac-only)
- **Python 3.6+** (included with Xcode Command Line Tools, or install via [Homebrew](https://brew.sh/))
- **No external dependencies** — uses only Python standard library modules
- **Network access** for reverse geocoding (optional, can be skipped with `--no-geocode`)

## Usage

### 1. Quit MacDive

The database cannot be modified while MacDive is open.

### 2. Preview changes (dry run)

```
python3 macdive_gps_backfill.py
```

This scans your database and shows what would be updated without making any changes:

```
=== DRY RUN (use --apply to write changes) ===

Found 10 Shearwater dive(s) without GPS:

  Dive 72: no Swift AI GPS in raw data — skipped
  Dive 73: no Swift AI GPS in raw data — skipped
  Dive 159: would update — Nadroga-Navosa, Fiji — Entry: -17.85270, 177.18138 / Exit: -17.85418, 177.18141
  Dive 160: would update — Nadroga-Navosa, Fiji — Entry: -17.86486, 177.19302 / Exit: -17.86238, 177.19233

Done. 2 dive(s) would be updated, 8 skipped (no GPS).
```

### 3. Apply changes

```
python3 macdive_gps_backfill.py --apply
```

A backup of your database is created automatically before any changes are written (`MacDive.sqlite.bak`).

### Options

| Flag | Description |
|------|-------------|
| `--apply` | Write changes to the database (default is dry-run) |
| `--no-geocode` | Skip reverse geocoding — still writes GPS coordinates but no country/region |
| `--db PATH` | Path to MacDive.sqlite (default: `~/Library/Application Support/MacDive/MacDive.sqlite`) |

## What Gets Written

### Dive Site (ZDIVESITE)

| Field | Value |
|-------|-------|
| ZGPSLAT | Entry latitude (decimal degrees) |
| ZGPSLON | Entry longitude (decimal degrees) |
| ZCOUNTRY | Country name (from geocoding) |
| ZLOCATION | Region/province name (from geocoding) |
| ZBODYOFWATER | Body of water, if resolved |

### Dive Notes (ZDIVE.ZNOTES)

Appended to any existing notes:

```
[Swift AI GPS] Nadroga-Navosa, Fiji — Entry: -17.85270, 177.18138 / Exit: -17.86238, 177.19233
```

The notes include both entry and exit coordinates since MacDive's dive site only supports a single location pin.

## How It Works

### Shearwater Native Format

Shearwater dive computers store dive logs in a proprietary binary format. Each dive is a sequence of 32-byte records, where byte 0 is the record type:

| Record Type | Name | Contents |
|-------------|------|----------|
| `0x14` | Opening Record 4 | Log version (byte +16), AI mode (byte +28) |
| `0x19` | Opening Record 9 | Entry GPS — lat at +21, lon at +25 |
| `0x29` | Closing Record 9 | Exit GPS — lat at +21, lon at +25 |

GPS coordinates are stored as **signed 32-bit big-endian integers**, divided by 100,000 to get decimal degrees (~1.1 meter precision).

The AI mode byte distinguishes transmitter types:

| Value | Mode | GPS |
|-------|------|-----|
| 0 | Off | No |
| 4 | HPCCR | No |
| 5 | AI On (Swift without GPS) | No |
| 6 | AI On GPS (Swift AI GPS) | **Yes** |

### Compression

MacDive stores the raw dive computer memory in the `ZRAWDATA` column. For Shearwater computers, this data is compressed:

1. **LRE (Length-Run Encoding):** Each 144-byte block is decoded as 9-bit packed values. Bit 8 set = literal byte, value 0 = end of block, otherwise = N zero bytes.
2. **XOR:** After all blocks are decoded, `data[i] ^= data[i-32]` for each byte from offset 32 onward.

### Geocoding

Reverse geocoding uses the [Nominatim](https://nominatim.openstreetmap.org/) API (OpenStreetMap). It's free with no API key required. The script respects the 1 request/second rate limit. Geocoding is best for coastal dive sites; mid-ocean coordinates may return only a country name or nothing.

## Compatibility

### Dive Computers

Any Shearwater computer that supports the Swift AI GPS transmitter:

- Perdix 2
- Petrel 3
- Teric
- Peregrine (with AI upgrade)
- Other current Shearwater models with AI support

Older Shearwater computers without AI support, or dives logged before the Swift AI GPS was paired, are detected and skipped automatically.

### MacDive Versions

Tested with MacDive 2.x. The script uses MacDive's Core Data SQLite schema (Z_-prefixed tables). If a future MacDive version changes the schema, the script will fail safely without corrupting data.

### Safety

- **Dry-run by default** — no changes unless you pass `--apply`
- **Automatic backup** — copies your database to `MacDive.sqlite.bak` before writing
- **Non-destructive** — only creates new dive site records and appends to notes; never modifies or deletes existing data
- **Idempotent** — running it again skips dives that already have GPS on their dive site

## Background

The Shearwater Swift AI GPS transmitter records GPS coordinates at the start and end of each dive and sends them to the paired dive computer. This data is stored in the dive log but is not exposed through Shearwater's standard export formats (XML, UDDF). The [Dive Shearwater](https://www.shearwater.com/pages/dive-shearwater) mobile app and Shearwater Cloud can display the GPS data, but MacDive has no way to extract it from the raw memory blob.

The GPS record format was identified from the [libdivecomputer](https://www.libdivecomputer.org/) open-source library, which added Swift AI GPS support in its Shearwater parser ([source](https://github.com/libdivecomputer/libdivecomputer/blob/master/src/shearwater_predator_parser.c)). The decompression algorithm (LRE + XOR) is also from libdivecomputer's Shearwater download implementation.

## License

MIT
