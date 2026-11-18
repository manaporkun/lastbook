# Istanbul (TR-34): a working offline map

Streets, building outlines, named places, emergency help points, earthquake assembly areas and street addresses for
Istanbul (`TR-34`), built from redistributable open sources, kept in its own folder, and searchable offline. It follows
the file formats in [MAPS.md](MAPS.md) and keeps its full ISO 3166-2 region code, `TR-34`.

This is a repeatable builder and testable map set, not an installer: the shared region download and setup picker are
still being coordinated in #25 and #31, so nothing here touches `packaging/`, the release manifest or the setup flow.

## Build it

```sh
python3 tools/build_region_tr_34.py fetch    # downloads the sources (~1.59 GB, mostly Geofabrik Turkey) into derived/regions/TR-34/source/
python3 tools/build_region_tr_34.py build    # writes derived/regions/TR-34/data/ (~7 min)
python3 tools/build_region_tr_34.py check    # validates checksums, bounds, SQLite integrity and search; exits non-zero on failure
python3 tools/build_region_tr_34.py demo     # adds an empty library beside it for trying the map alone
python3 server.py --data derived/regions/TR-34/demo --no-index
```

Python 3.9 or newer and its standard library; no GDAL, pyosmium or third-party packages. `fetch` is the only step that
uses the network. Everything is written under `derived/regions/TR-34/`, so no existing map set changes. `build.json`
records every source (URL, dated file name, data date, licence, size, SHA-256) and every output (size, SHA-256).

## Sources

| What | Source | Licence | Attribution |
|---|---|---|---|
| Streets, water, land use, buildings, named places and points | OpenStreetMap via Geofabrik, `turkey-YYMMDD-free.gpkg.zip` (clipped to Istanbul `27.82, 40.65, 30.08, 41.74`) | ODbL 1.0 | © OpenStreetMap contributors (already on the map) |
| Street addresses (`addr:housenumber` + `addr:street`) and disaster assembly areas (`emergency=assembly_point`) | OpenStreetMap via Overpass API inside `ISO3166-2=TR-34` | ODbL 1.0 | © OpenStreetMap contributors |
| Hospitals, clinics, family health centres, doctors, dentists, pharmacies, vets and care homes | Istanbul Metropolitan Municipality (İBB) Open Data Portal, `saglik-tesisleri.xlsx` | İBB Open Data License v1.0 (CC BY 4.0 / ODC-By compatible) | İstanbul Büyükşehir Belediyesi Açık Veri Portalı (CC BY 4.0) |
| Fire stations (136 stations) | İBB Open Data Portal, `itfaiye-istasyon-konumlar.2025.xlsx` | İBB Open Data License v1.0 (CC BY 4.0 compatible) | İstanbul Büyükşehir Belediyesi Açık Veri Portalı (CC BY 4.0) |
| Neighbourhood headmen offices (*muhtarlık*, 984 records) and *mahalle* centres | İBB Open Data Portal, `muhtarlik_lokasyon.geojson` | İBB Open Data License v1.0 (CC BY 4.0 compatible) | İstanbul Büyükşehir Belediyesi Açık Veri Portalı (CC BY 4.0) |
| Public toilets and active fuel stations | İBB Open Data Portal, `sehir_tuvaletleri_verisi.geojson`, `akaryakt_istasyonlar_veri_seti_20261012t130600.csv` | İBB Open Data License v1.0 (CC BY 4.0 compatible) | İstanbul Büyükşehir Belediyesi Açık Veri Portalı (CC BY 4.0) |
| Province (`TUR034`) and 39 district (*ilçe*) boundaries | OCHA HDX COD-AB Türkiye (`tur_admin_boundaries.geojson.zip`, from General Command of Mapping / HGM) | CC BY-IGO | OCHA / General Command of Mapping (HGM) |

For hospitals, pharmacies and fire stations, İBB's official municipal registers replace OpenStreetMap's points inside
Istanbul so each facility appears once from the public register. Clinics, doctors, dentists, vets, care homes,
*muhtarlık* offices, public toilets and fuel stations supplement OpenStreetMap, deduplicated at display time within
80 m by `server.py`.

## What the build holds

Built on 17 November 2026 from Geofabrik's `turkey-261116-free.gpkg.zip` (OSM data as of `2026-11-16T21:22:36Z`),
Overpass (`2026-11-17T17:55:37Z`), İBB Open Data Portal datasets and OCHA COD-AB boundaries.

| File | Size | SHA-256 (this build) | Holds |
|---|---|---|---|
| `derived/geo/national.sqlite` | 235.8 MB | `f9fc6e8d…7cdaab` | 330,581 road features, 915,991 building outlines, water, land, rail and places; help and supply points |
| `derived/geo/map.sqlite` | 32.8 KB | `7b7c424e…5db44` | empty, as in a US state map; carries the map's source, bounds (`27.9743, 40.8027, 29.9217, 41.5835`) and date |
| `derived/geo/places.sqlite` | 8.3 MB | `095b97d1…6297e` | 1 province, 39 districts (*ilçe*), 885 neighbourhoods (*mahalle*), 351 postcode areas, 29,907 streets, 30,343 named points, 1,644 named places |
| `derived/geo/addresses/TR-34.sqlite` | 6.1 MB | `10ad3b99…924b0` | 51,482 civic addresses (`meta.norm = tr-1`) from OpenStreetMap and İBB civic registers |
| `map-set.json` | 192 B | `78e3ed70…81f19` | name `TR-34` and the sizes of the map files |

Help points include 547 hospitals, 5,806 pharmacies and 136 fire stations from İBB (replacing 416, 5,052 and 124
OpenStreetMap points), 2,136 clinics, 3,425 dentists, 244 police stations, 217 drinking-water taps, and 132 mapped
open-air earthquake assembly points (`assembly_point`).

## Checks

`check` verifies, and exits non-zero on any failure:

- every output file matches the size and SHA-256 recorded at build time; `map-set.json` is named `TR-34` and its sizes match;
- every SQLite file passes `PRAGMA integrity_check`;
- road features (>100,000), building outlines (>100,000) and each emergency help class meet expected Istanbul counts;
- every help point and address lies within Istanbul's bounds, and every present postcode is five digits;
- sampled Istanbul addresses resolve exactly through `engine/address_search.py`, and an unknown door number returns nothing;
- the place finder resolves province, district (*ilçe*) and neighbourhood (*mahalle*) entries, including ASCII `Kadikoy` -> `Kadıköy`.

## Try it

With the demo server running, open Kadıköy or Beyoğlu:

- `http://127.0.0.1:8890/#/map?lat=40.9905&lon=29.0265&z=15&layers=hospital,pharmacy,fire,police,assembly,water`
- `http://127.0.0.1:8890/#/map?lat=41.0425&lon=28.9720&z=15&layers=hospital,pharmacy,fire,police,assembly,water`

| Search | Result |
|---|---|
| `Bağdat Caddesi No: 75 Kadıköy` | Bağdat Caddesi No: 75 (`Feneryolu, Kadıköy, 34724`) |
| `Bagdat Cad. 75, 34724 Kadikoy` | Bağdat Caddesi No: 75 (Turkish `ğ/ı/ö/İ` and `Cad.`/`Cd.` folded) |
| `Süleyman Seba Caddesi No: 22 Beşiktaş` | Süleyman Seba Caddesi No: 22 |
| `Bağdat Caddesi No: 99999 Kadıköy` | no house is guessed; falls back to the street/place search |
| `Kadikoy`, `Caferağa`, `34710 Kadıköy` | the district, the *mahalle*, the place |
| `Route 66` | still a place search, as before |

## Changes outside the builder

- `tools/build_map.py`: `--source`, `--as-of`, `--bounds`, `--clip` (using GeoPackage binary envelope filtering before WKB decoding) and `--extra-poi`. Defaults and Southern California output are unchanged.
- `engine/address_search.py` and `server.py` (`places`): Turkish street-first order (`Cadde/Sokak/Bulvar [No:] KapıNo [PostaKodu] [Mahalle/İlçe]`) for indexes whose `meta` says `norm = tr-1`, folding Turkish dotted/dotless `İ/I/ı`, circumflex `â/î/û`, and `ğ/ü/ş/ö/ç`. SQLite's `unicode61` tokenizer treats dotless `ı` as distinct from `i`, so `places()` expands `i`/`ı` FTS prefix variants so ASCII `Kadikoy` finds `Kadıköy`. Published US indexes and US queries behave identically to `main`.
- `web/map.js` and `server.py` (`HELP`, `TILE_POI`): an `assembly_point` class and a "Disaster assembly areas (open air)" layer (`A`) in the Emergency help group, with an explicit caveat that mapped assembly areas are open-air gathering points rather than indoor shelters.

## Limits

- **Turkey's national address register (UAVT / MERNİS) is not open data**, and Overture Maps lists no address source for Turkey. The `TR-34` address index (51,482 addresses) combines OpenStreetMap `addr:*` records with civic addresses parsed from İBB's public facility and *muhtarlık* registers; streets without mapped building numbers still resolve at street or *mahalle* level in the place finder (29,907 streets, 885 *mahalleler*).
- **Earthquake assembly points (`assembly_point`) come from OpenStreetMap (132 mapped in Istanbul).** AFAD / e-Devlet publishes official assembly areas behind interactive login rather than a bulk open file, and İBB's open portal does not currently publish the full assembly-area GeoJSON.
- **The app's fixed chrome is still US-oriented** (headline date, "ZIP" placeholder, "US" whole-map button until `/api/map/info` switches it, and on-map US agency credit line), left to the shared region work in #25 and #31.
- **Not in this map:** terrain (`terrain.sqlite`), US crop layers (`crops.sqlite`), dam storage (`water.sqlite`) and the US resource atlas.
