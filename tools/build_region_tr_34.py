#!/usr/bin/env python3
"""Istanbul (TR-34): a self-contained offline map set with places, help points and street addresses.

  python3 tools/build_region_tr_34.py fetch    # downloads the sources into derived/regions/TR-34/source/
  python3 tools/build_region_tr_34.py build    # writes the data folder derived/regions/TR-34/data/
  python3 tools/build_region_tr_34.py check    # validates it; exits non-zero on any failure
  python3 tools/build_region_tr_34.py demo     # adds an empty library beside it, then prints the command below
  python3 server.py --data derived/regions/TR-34/demo --no-index   # the map, place and address search, offline

Everything goes under derived/regions/TR-34/, so no existing map set is touched. The region keeps its ISO 3166-2
code, TR-34. What this build holds and what it lacks: docs/MAP-TR-34.md.

Sources (all redistributable; details, dates and checksums end up in derived/regions/TR-34/build.json):
  - OpenStreetMap via Geofabrik, turkey-YYMMDD-free.gpkg.zip: streets, water, land use, buildings, named places and
    points clipped to Istanbul (ODbL 1.0, "© OpenStreetMap contributors").
  - OpenStreetMap via Overpass API inside ISO3166-2=TR-34: street addresses (addr:housenumber + addr:street) and
    earthquake/evacuation assembly points (emergency=assembly_point) (ODbL 1.0, "© OpenStreetMap contributors").
  - Istanbul Metropolitan Municipality (İBB) Open Data Portal (data.ibb.gov.tr), İBB Open Data License v1.0
    (CC BY 4.0 / ODC-By compatible):
      * Health institutions (saglik-tesisleri.xlsx): hospitals, clinics, doctors, dentists, pharmacies, vets, care homes
      * Fire stations (itfaiye-istasyon-konumlar.2025.xlsx): 136 municipal fire stations
      * Neighbourhood headmen offices (muhtarlik_lokasyon.geojson): 984 mahalle headmen offices and locality centres
      * Public toilets (sehir_tuvaletleri_verisi.geojson): municipal public toilets
      * Fuel stations (akaryakt_istasyonlar_veri_seti_20261012t130600.csv): active licensed fuel stations
  - OCHA HDX COD-AB Türkiye administrative boundaries (tur_admin_boundaries.geojson.zip, General Command of Mapping,
    CC BY-IGO): official province boundary (TUR034) and 39 district (ilçe) boundaries.

The fetch step needs the network; build and check do not. Python 3.9+ standard library only.
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REGION = 'TR-34'
BASE = ROOT / 'derived' / 'regions' / REGION
SOURCE = BASE / 'source'
DATA = BASE / 'data'
GEO = DATA / 'derived' / 'geo'
REPORT = BASE / 'build.json'

# Exact land boundary of Istanbul (TUR034) from OCHA/HGM COD-AB: (27.9743, 40.8027, 29.9217, 41.5835).
# Padded by 0.15° like map_sets.state so roads and water across the provincial edge do not cut off abruptly.
LAND_BBOX = (27.9743, 40.8027, 29.9217, 41.5835)
CLIP_BBOX = (27.82, 40.65, 30.08, 41.74)

GEOFABRIK = 'https://download.geofabrik.de/europe/turkey-latest-free.gpkg.zip'
OVERPASS_ENDPOINTS = (
    'https://overpass-api.de/api/interpreter',
    'https://lz4.overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
)
IBB_LICENCE = 'İBB Open Data License v1.0 (CC BY 4.0 compatible, https://data.ibb.gov.tr/license)'
IBB_ATTR = 'İstanbul Büyükşehir Belediyesi Açık Veri Portalı (CC BY 4.0)'

DOWNLOADS = {
    'ibb_health.xlsx': (
        'https://data.ibb.gov.tr/dataset/bd3b9489-c7d5-4ff3-897c-8667f57c70bb/resource/f2154883-68e3-41dc-b2be-a6c2eb721c9e/download/saglik-tesisleri.xlsx',
        IBB_LICENCE, IBB_ATTR,
    ),
    'ibb_fire.xlsx': (
        'https://data.ibb.gov.tr/dataset/75cd7b09-dafb-41fa-90c9-9c7396a58700/resource/d3efb4ce-90b4-46b6-a96e-bbbe5b1e0234/download/itfaiye-istasyon-konumlar.2025.xlsx',
        IBB_LICENCE, IBB_ATTR,
    ),
    'ibb_muhtarlik.geojson': (
        'https://data.ibb.gov.tr/dataset/c310cde9-92b1-4c51-9575-d71b1dc7ac43/resource/71f75529-7fae-4a85-b05f-664c62eda422/download/muhtarlik_lokasyon.geojson',
        IBB_LICENCE, IBB_ATTR,
    ),
    'ibb_toilets.geojson': (
        'https://data.ibb.gov.tr/dataset/8789b75c-bae9-4acb-8287-bfc0ba88c8ea/resource/9585ab4a-d160-4541-8a41-b86eeec4c1cc/download/sehir_tuvaletleri_verisi.geojson',
        IBB_LICENCE, IBB_ATTR,
    ),
    'ibb_fuel.csv': (
        'https://data.ibb.gov.tr/dataset/271839be-25be-479b-abde-ec23f242ef2a/resource/5625860c-d79a-446f-898e-2aa2b9099bc8/download/akaryakt_istasyonlar_veri_seti_20261012t130600.csv',
        IBB_LICENCE, IBB_ATTR,
    ),
    'tur_admin.geojson.zip': (
        'https://data.humdata.org/dataset/d74086a0-f398-4474-9e12-1b9a70907bd0/resource/470bd810-2240-4ce0-b5c4-17434112ce41/download/tur_admin_boundaries.geojson.zip',
        'Creative Commons Attribution for Intergovernmental Organisations (CC BY-IGO)',
        'OCHA / General Command of Mapping (HGM)',
    ),
}

HEALTH_MAP = {
    'Devlet Hastanesi': 'hospital',
    'Şehir Hastanesi': 'hospital',
    'Eğitim Araştırma Hastanesi': 'hospital',
    'Üniversite Hastanesi': 'hospital',
    'Kadın Doğum ve Çocuk Hastanesi': 'hospital',
    'Özel Hastane': 'hospital',
    'Acil Yardım İstasyonu': 'hospital',
    'Aile Sağlığı Merkezi': 'clinic',
    'Toplum Sağlığı Merkezi': 'clinic',
    'Belediye Sağlık Merkezi': 'clinic',
    'Tıp Merkezi Özel': 'clinic',
    'Poliklinik Özel': 'clinic',
    'Semt Poliklinikleri': 'clinic',
    'Verem Savaş Dispanseri': 'clinic',
    'Kızılay/Kan Merkezi': 'clinic',
    'Diyaliz Merkezi': 'clinic',
    'Diyaliz Merkezi Özel': 'clinic',
    'Fizik Tedavi ve Rehabilitasyon Merkezi': 'clinic',
    'Fizik Tedavi ve Rehabilitasyon Merkezi Özel': 'clinic',
    'Doktor/Muayenehane': 'doctors',
    'Eczane': 'pharmacy',
    'Diş Hekimi': 'dentist',
    'Ağız Diş Sağlığı Merkezi': 'dentist',
    'Özel Ağız ve Diş Sağlığı Merkezleri': 'dentist',
    'Veteriner': 'veterinary',
    'Yaşlı Bakım Evi/Huzurevi': 'nursing_home',
}
OFFICIAL_REPLACED = ('hospital', 'pharmacy', 'fire_station')


def log(*args):
    print(time.strftime('%H:%M:%S'), *args, flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 22), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_to(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'lastbook-region-builder'})
    with urllib.request.urlopen(req, timeout=600) as resp:
        final = resp.geturl()
        tmp = dest.with_suffix(dest.suffix + '.part')
        with open(tmp, 'wb') as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        tmp.replace(dest)
    return final


def overpass_fetch(query):
    payload = urllib.parse.urlencode({'data': query}).encode('utf-8')
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(endpoint, data=payload, headers={'User-Agent': 'lastbook-region-builder'})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return endpoint, json.loads(resp.read().decode('utf-8'))
            except (urllib.error.URLError, TimeoutError, ValueError) as err:
                last_err = err
                time.sleep(3 * (attempt + 1))
    raise SystemExit(f'Overpass query failed across mirrors: {last_err}')


# ------------------------------------------------------------------ fetch ----

def fetch():
    SOURCE.mkdir(parents=True, exist_ok=True)
    fetched = {'fetched': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), 'files': {}}

    osm_zip = SOURCE / 'osm.gpkg.zip'
    if osm_zip.is_file() and zipfile.is_zipfile(osm_zip):
        log('Geofabrik (cached)', osm_zip.name, osm_zip.stat().st_size)
        req = urllib.request.Request(GEOFABRIK, method='HEAD', headers={'User-Agent': 'lastbook-region-builder'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
        except Exception:
            final_url = GEOFABRIK
    else:
        log('Geofabrik', GEOFABRIK)
        final_url = download_to(GEOFABRIK, osm_zip)
    dated_name = final_url.rsplit('/', 1)[-1]
    with zipfile.ZipFile(osm_zip) as archive:
        readme = archive.read('README').decode('utf-8', 'replace')
    as_of = re.search(r'data as of (\S+)', readme)
    fetched['files']['osm.gpkg.zip'] = {
        'url': final_url, 'name': dated_name, 'data_as_of': as_of.group(1) if as_of else None,
        'licence': 'Open Database License 1.0 (ODbL)', 'attribution': '© OpenStreetMap contributors',
        'bytes': osm_zip.stat().st_size, 'sha256': sha256(osm_zip),
    }

    for filename, (url, licence, attribution) in DOWNLOADS.items():
        dest = SOURCE / filename
        log('downloading', filename)
        download_to(url, dest)
        fetched['files'][filename] = {
            'url': url, 'licence': licence, 'attribution': attribution,
            'bytes': dest.stat().st_size, 'sha256': sha256(dest),
        }

    log('Overpass: Istanbul addresses and emergency assembly points')
    addr_query = (
        '[out:json][timeout:180];'
        'area["ISO3166-2"="TR-34"]->.ist;'
        '(nwr(area.ist)["addr:housenumber"]["addr:street"];'
        ' nwr(area.ist)["emergency"="assembly_point"];);'
        'out center tags;'
    )
    endpoint, ov_data = overpass_fetch(addr_query)
    ov_path = SOURCE / 'osm_istanbul_extra.json'
    ov_path.write_text(json.dumps(ov_data, ensure_ascii=False), encoding='utf-8')
    fetched['files'][ov_path.name] = {
        'url': endpoint, 'data_as_of': ov_data.get('osm3s', {}).get('timestamp_osm_base'),
        'elements': len(ov_data.get('elements', [])),
        'licence': 'Open Database License 1.0 (ODbL)', 'attribution': '© OpenStreetMap contributors',
        'bytes': ov_path.stat().st_size, 'sha256': sha256(ov_path),
    }

    (SOURCE / 'fetched.json').write_text(json.dumps(fetched, indent=1, ensure_ascii=False), encoding='utf-8')
    log('fetched', len(fetched['files']), 'files')


# ------------------------------------------------------------------ helpers ----

def tr_title(text):
    """Convert UPPERCASE Turkish administrative names ('KADIKÖY', 'ŞİŞLİ', 'İSTANBUL') to proper Turkish title case."""
    tr_lower_map = str.maketrans({'İ': 'i', 'I': 'ı', 'Ğ': 'ğ', 'Ü': 'ü', 'Ş': 'ş', 'Ö': 'ö', 'Ç': 'ç'})
    tr_upper_first = {'i': 'İ', 'ı': 'I', 'ğ': 'Ğ', 'ü': 'Ü', 'ş': 'Ş', 'ö': 'Ö', 'ç': 'Ç'}
    words = []
    for part in (text or '').strip().split():
        low = part.translate(tr_lower_map).lower()
        if low:
            first = tr_upper_first.get(low[0], low[0].upper())
            words.append(first + low[1:])
    return ' '.join(words)


def read_xlsx_rows(path):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    col_re = re.compile(r'([A-Z]+)')
    with zipfile.ZipFile(path) as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            shared = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in root.findall('m:si', ns)]
        sheet_name = sorted(n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))[0]
        root = ET.fromstring(archive.read(sheet_name))
        out = []
        for row in root.findall('.//m:row', ns):
            cells = {}
            for cell in row.findall('m:c', ns):
                ref = cell.attrib.get('r', '')
                m = col_re.match(ref)
                idx = 0
                if m:
                    for ch in m.group(1):
                        idx = idx * 26 + (ord(ch) - ord('A') + 1)
                    idx -= 1
                else:
                    idx = len(cells)
                ctype = cell.attrib.get('t')
                val_el = cell.find('m:v', ns)
                if val_el is not None and val_el.text is not None:
                    cells[idx] = shared[int(val_el.text)] if ctype == 's' else val_el.text
                else:
                    inl = cell.find('m:is', ns)
                    cells[idx] = ''.join(x.text or '' for x in inl.findall('.//m:t', ns)) if inl is not None else ''
            if cells:
                width = max(cells) + 1
                out.append([cells.get(i, '') for i in range(width)])
        return out


def _ring_box(ring):
    xs = [pt[0] for pt in ring]
    ys = [pt[1] for pt in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _prep_rings(polys):
    out = []
    for poly in polys:
        outer = poly[0]
        holes = [(h, _ring_box(h)) for h in poly[1:]]
        out.append((outer, _ring_box(outer), holes))
    return out


def point_in_Rings(lon, lat, rings):
    """Ray-casting check against prepared (outer, bbox, holes) polygon rings."""
    for outer, (x0, y0, x1, y1), holes in rings:
        if lon < x0 or lon > x1 or lat < y0 or lat > y1:
            continue
        if _pip(lon, lat, outer) and not any(
            hx0 <= lon <= hx1 and hy0 <= lat <= hy1 and _pip(lon, lat, h)
            for h, (hx0, hy0, hx1, hy1) in holes
        ):
            return True
    return False


def _pip(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def load_admin():
    """Return (istanbul_rings, district_rows[(name, lat, lon, box, rings)])."""
    from engine.address_search import fold_tr
    muhtar = json.loads((SOURCE / 'ibb_muhtarlik.geojson').read_text(encoding='utf-8'))['features']
    canonical_ilce = {}
    for feat in muhtar:
        raw = (feat.get('properties') or {}).get('İlçe Adı')
        if raw:
            canonical_ilce[fold_tr(raw).replace(' ', '')] = tr_title(raw)
    with zipfile.ZipFile(SOURCE / 'tur_admin.geojson.zip') as z:
        adm1 = json.loads(z.read('tur_admin1.geojson'))
        adm2 = json.loads(z.read('tur_admin2.geojson'))
    ist_rings = []
    for feat in adm1['features']:
        if feat['properties'].get('adm1_pcode') == 'TUR034':
            geom = feat['geometry']
            polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
            ist_rings = _prep_rings(polys)
            break
    districts = []
    for feat in adm2['features']:
        props = feat['properties']
        if props.get('adm1_pcode') == 'TUR034':
            raw_name = (props.get('adm2_name1') or props.get('adm2_name') or '').replace('\u0307', '')
            name = canonical_ilce.get(fold_tr(raw_name).replace(' ', ''), raw_name)
            geom = feat['geometry']
            polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
            rings = _prep_rings(polys)
            box = (
                min(b[0] for _, b, _ in rings), min(b[1] for _, b, _ in rings),
                max(b[2] for _, b, _ in rings), max(b[3] for _, b, _ in rings),
            )
            districts.append((name, float(props['center_lat']), float(props['center_lon']), box, rings))
    return ist_rings, districts

def district_for(lon, lat, districts):
    for name, _, _, (x0, y0, x1, y1), rings in districts:
        if x0 <= lon <= x1 and y0 <= lat <= y1 and point_in_Rings(lon, lat, rings):
            return name
    return ''


def in_clip(lon, lat):
    return CLIP_BBOX[0] <= lon <= CLIP_BBOX[2] and CLIP_BBOX[1] <= lat <= CLIP_BBOX[3]


# ------------------------------------------------------------------ build ----

def build_map_db(fetched, ist_rings):
    osm = fetched['files']['osm.gpkg.zip']
    work = BASE / 'work'
    work.mkdir(parents=True, exist_ok=True)
    gpkg = work / 'turkey.gpkg'
    if not gpkg.exists():
        with zipfile.ZipFile(SOURCE / 'osm.gpkg.zip') as archive:
            member = next(n for n in archive.namelist() if n.endswith('.gpkg'))
            with archive.open(member) as src, open(gpkg, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1 << 22)
    national = GEO / 'national.sqlite'
    subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'build_map.py'), str(gpkg), str(national),
         '--source', f"OpenStreetMap contributors, via Geofabrik {osm['name']} (ODbL 1.0); İBB Open Data Portal (CC BY 4.0)",
         '--as-of', osm['data_as_of'] or '',
         '--bounds', ','.join(map(str, LAND_BBOX)),
         '--clip', ','.join(map(str, CLIP_BBOX))],
        check=True, stdout=subprocess.DEVNULL,
    )

    extra = json.loads((SOURCE / 'osm_istanbul_extra.json').read_text(encoding='utf-8'))['elements']
    assembly_rows = []
    for el in extra:
        tags = el.get('tags') or {}
        if tags.get('emergency') == 'assembly_point':
            lat = el.get('lat') or (el.get('center') or {}).get('lat')
            lon = el.get('lon') or (el.get('center') or {}).get('lon')
            if lat is not None and lon is not None and in_clip(lon, lat):
                assembly_rows.append(('assembly_point', tags.get('name') or 'Afet Toplanma Alanı', lon, lat))

    health_rows = read_xlsx_rows(SOURCE / 'ibb_health.xlsx')
    ibb_pois = []
    health_counts = {}
    for row in health_rows[1:]:
        if len(row) < 8:
            continue
        name, _, subcat, _, _, _, lat_s, lon_s = row[:8]
        cls = HEALTH_MAP.get(subcat.strip())
        if not cls:
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        if in_clip(lon, lat):
            ibb_pois.append((cls, name.strip() or None, lon, lat))
            health_counts[cls] = health_counts.get(cls, 0) + 1

    fire_rows = read_xlsx_rows(SOURCE / 'ibb_fire.xlsx')
    fire_count = 0
    for row in fire_rows[1:]:
        if len(row) < 6:
            continue
        name, _, _, _, lon_s, lat_s = row[:6]     # İBB sheet stores Koordinat(Y)=lon, Koordinat(X)=lat
        try:
            lon, lat = float(lon_s), float(lat_s)
        except ValueError:
            continue
        if in_clip(lon, lat):
            ibb_pois.append(('fire_station', name.strip() or 'İtfaiye İstasyonu', lon, lat))
            fire_count += 1

    muhtar = json.loads((SOURCE / 'ibb_muhtarlik.geojson').read_text(encoding='utf-8'))['features']
    muhtar_count = 0
    for feat in muhtar:
        if not feat.get('geometry'):
            continue
        lon, lat = feat['geometry']['coordinates'][:2]
        if in_clip(lon, lat):
            name = (feat['properties'].get('Muhtarlık Adı') or '').strip()
            ibb_pois.append(('town_hall', name or 'Muhtarlık', lon, lat))
            muhtar_count += 1

    toilets = json.loads((SOURCE / 'ibb_toilets.geojson').read_text(encoding='utf-8'))['features']
    toilet_count = 0
    for feat in toilets:
        if not feat.get('geometry'):
            continue
        props = feat.get('properties') or {}
        if props.get('TUVALET_DURUM') and props['TUVALET_DURUM'] != 'Aktif':
            continue
        lon, lat = feat['geometry']['coordinates'][:2]
        if in_clip(lon, lat):
            mah = tr_title(props.get('MAHAL_ADI') or '')
            ibb_pois.append(('toilet', f'{mah} Şehir Tuvaleti'.strip(), lon, lat))
            toilet_count += 1

    fuel_count = 0
    with open(SOURCE / 'ibb_fuel.csv', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if (row.get('facility_status') or '').strip() not in ('Aktif', ''):
                continue
            try:
                lat, lon = float(row['latitude']), float(row['longitude'])
            except (KeyError, ValueError):
                continue
            if in_clip(lon, lat):
                dist = tr_title(row.get('district_name') or '')
                ibb_pois.append(('fuel', f'Akaryakıt İstasyonu ({dist})' if dist else 'Akaryakıt İstasyonu', lon, lat))
                fuel_count += 1

    db = sqlite3.connect(national)
    try:
        # Remove OSM POIs outside Istanbul's official province polygon (e.g. Kocaeli, Tekirdağ, Yalova across the clip box)
        all_pois = db.execute('SELECT id, lon, lat FROM poi').fetchall()
        outside_ids = [(pid,) for pid, lon, lat in all_pois if not point_in_Rings(lon, lat, ist_rings)]
        if outside_ids:
            db.executemany('DELETE FROM poi WHERE id=?', outside_ids)
        replaced = dict(db.execute(
            f"SELECT cls, count(*) FROM poi WHERE cls IN ({','.join('?' * len(OFFICIAL_REPLACED))}) GROUP BY cls",
            OFFICIAL_REPLACED,
        ).fetchall())
        db.execute(f"DELETE FROM poi WHERE cls IN ({','.join('?' * len(OFFICIAL_REPLACED))})", OFFICIAL_REPLACED)
        db.executemany('INSERT INTO poi(cls,name,lon,lat) VALUES (?,?,?,?)', ibb_pois + assembly_rows)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('help_points', ?)", (
            'hospital, pharmacy, fire_station: İBB Open Data Portal (CC BY 4.0), replacing OpenStreetMap; '
            'clinic, doctors, dentist, veterinary, nursing_home, town_hall (muhtarlık), toilet, fuel: İBB + OpenStreetMap; '
            'assembly_point and all other classes: OpenStreetMap (ODbL 1.0)',
        ))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('region', ?)", (REGION,))
        db.commit()
        db.execute('VACUUM')
    finally:
        db.close()

    empty = sqlite3.connect(GEO / 'map.sqlite')
    try:
        empty.executescript('''CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz);
          CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);
          CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);''')
        meta_db = sqlite3.connect(f'file:{national}?mode=ro', uri=True)
        try:
            empty.executemany('INSERT INTO meta VALUES (?,?)', meta_db.execute(
                "SELECT key,value FROM meta WHERE key IN ('source','data_as_of','bounds','help_points','region')"
            ).fetchall())
        finally:
            meta_db.close()
        empty.commit()
    finally:
        empty.close()

    gpkg.unlink(missing_ok=True)
    return {
        'osm_points_replaced_by_ibb': replaced,
        'ibb_health_points': health_counts,
        'ibb_fire_stations': fire_count,
        'ibb_muhtarlik': muhtar_count,
        'ibb_public_toilets': toilet_count,
        'ibb_fuel_stations': fuel_count,
        'osm_assembly_points': len(assembly_rows),
    }


def clean_house_number(raw):
    raw = (raw or '').strip()
    raw = re.sub(r'^(?:no|numara)\s*[:.]?\s*', '', raw, flags=re.I)
    m = re.match(r'^0*(\d+)\s*[/\s-]?\s*([A-Za-z])?$', raw)
    if m:
        return f"{m.group(1)}{(m.group(2) or '').upper()}"
    m2 = re.match(r'^0*(\d+)\s*[-–]\s*\d+$', raw)
    if m2:
        return m2.group(1)
    return ''


def clean_street_name(street, district):
    street = re.sub(r'\s+', ' ', (street or '').replace('\u0307', '').strip(' ,'))
    if ',' in street or re.search(r'\bno\s*[:.]?\s*\d|\b\d{5}\b', street, flags=re.I):
        return ''
    if district and street.lower().endswith(' ' + district.lower()):
        street = street[:-len(district)].strip(' ,')
    return street if len(street) <= 80 else ''

def parse_ibb_address(addr_text):
    """Extract (street, house_number) from İBB strings such as 'Çayır Cad. No:25 F YALIKÖY/BEYKOZ'."""
    if not addr_text:
        return '', ''
    m = re.search(r'^(.+?\b(?:Cad\.|Caddesi|Sk\.|Sok\.|Sokak|Sokağı|Bulvarı|Bulv\.|Blv\.|Meydanı|Yolu))\s*No\s*[:.]?\s*(\d+(?:\s*[/\s-]\s*[A-Za-z])?)', addr_text.strip(), flags=re.I)
    if not m:
        return '', ''
    street = re.sub(r'^.+?\b(?:Mah\.|Mahallesi|Mh\.)\s*', '', m.group(1).strip(), flags=re.I)
    return street.strip(' ,'), clean_house_number(m.group(2))


def build_addresses(fetched, ist_rings, districts):
    from engine.address_search import normalize_tr
    path = GEO / 'addresses' / f'{REGION}.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = json.loads((SOURCE / 'osm_istanbul_extra.json').read_text(encoding='utf-8'))['elements']
    rows = []
    seen = set()
    skipped = 0

    for el in extra:
        tags = el.get('tags') or {}
        if 'addr:housenumber' not in tags or 'addr:street' not in tags:
            continue
        lat = el.get('lat') or (el.get('center') or {}).get('lat')
        lon = el.get('lon') or (el.get('center') or {}).get('lon')
        if lat is None or lon is None or not point_in_Rings(lon, lat, ist_rings):
            skipped += 1
            continue
        num = clean_house_number(tags.get('addr:housenumber'))
        dist = (tags.get('addr:district') or tags.get('addr:town') or '').strip()
        if not dist or dist.lower() in ('istanbul', 'i̇stanbul'):
            dist = district_for(lon, lat, districts)
        else:
            dist = tr_title(dist)
        street = clean_street_name(tags.get('addr:street'), dist)
        norm = normalize_tr(street)
        if not num or not street or not norm:
            skipped += 1
            continue
        mah = (tags.get('addr:neighbourhood') or tags.get('addr:suburb') or tags.get('addr:quarter') or '').strip()
        mah = re.sub(r'\s*(?:Mahallesi|Mah\.?|Mh\.?)$', '', mah, flags=re.I).strip()
        mah = tr_title(mah) if mah else ''
        postcode = (tags.get('addr:postcode') or '').strip()
        if not re.fullmatch(r'\d{5}', postcode):
            postcode = ''
        key = (num, norm, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        rows.append((num, street, norm, dist or 'İstanbul', REGION, postcode, round(lat, 6), round(lon, 6), mah))

    # Supplement with parsed civic addresses from İBB muhtarlık, health and fire station registers
    muhtar = json.loads((SOURCE / 'ibb_muhtarlik.geojson').read_text(encoding='utf-8'))['features']
    for feat in muhtar:
        if not feat.get('geometry'):
            continue
        lon, lat = feat['geometry']['coordinates'][:2]
        if not point_in_Rings(lon, lat, ist_rings):
            continue
        p = feat['properties']
        street, num = parse_ibb_address(p.get('Adres') or '')
        norm = normalize_tr(street)
        if num and street and norm:
            dist = tr_title(p.get('İlçe Adı') or '') or district_for(lon, lat, districts)
            mah = tr_title(p.get('Mahalle Adı') or '')
            key = (num, norm, round(lat, 5), round(lon, 5))
            if key not in seen:
                seen.add(key)
                rows.append((num, street, norm, dist or 'İstanbul', REGION, '', round(lat, 6), round(lon, 6), mah))

    for row in read_xlsx_rows(SOURCE / 'ibb_health.xlsx')[1:]:
        if len(row) < 8:
            continue
        _, _, _, ilce, mah_raw, adres, lat_s, lon_s = row[:8]
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        if not point_in_Rings(lon, lat, ist_rings):
            continue
        street, num = parse_ibb_address(adres)
        norm = normalize_tr(street)
        if num and street and norm:
            key = (num, norm, round(lat, 5), round(lon, 5))
            if key not in seen:
                seen.add(key)
                rows.append((num, street, norm, tr_title(ilce) or district_for(lon, lat, districts), REGION, '',
                             round(lat, 6), round(lon, 6), tr_title(mah_raw)))

    db = sqlite3.connect(path)
    try:
        db.executescript(
            'PRAGMA page_size=8192; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;'
            'CREATE TABLE addr(num TEXT, street TEXT, norm TEXT, city TEXT, state TEXT, zip TEXT, lat REAL, lon REAL, district TEXT);'
            'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);'
        )
        db.executemany('INSERT INTO addr VALUES (?,?,?,?,?,?,?,?,?)', rows)
        db.execute('CREATE INDEX addr_norm ON addr(norm, num)')
        db.execute('CREATE INDEX addr_zip ON addr(zip)')
        db.executemany('INSERT INTO meta VALUES (?,?)', [
            ('region', REGION), ('norm', 'tr-1'), ('count', str(len(rows))),
            ('source', 'OpenStreetMap (ODbL 1.0) & İBB Open Data Portal (CC BY 4.0)'),
            ('data_as_of', fetched['files']['osm_istanbul_extra.json'].get('data_as_of') or ''),
            ('built', time.strftime('%Y-%m-%dT%H:%M:%S')),
        ])
        db.commit()
    finally:
        db.close()
    return {'addresses': len(rows), 'address_records_skipped': skipped}


def build_places(ist_rings, districts):
    path = GEO / 'places.sqlite'
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    counts = {}
    try:
        db.executescript(
            "CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);"
            "CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');"
        )
        insert = 'INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)'

        def add(kind, items):
            db.executemany(insert, items)
            counts[kind] = len(items)

        add('province', [('İstanbul', 'province (il)', 'Türkiye (TR-34)', 41.0082, 28.9784, 0, 'OCHA / HGM')])
        add('district', [(name, 'district (ilçe)', 'İstanbul', lat, lon, 1, 'OCHA / HGM') for name, lat, lon, _, _ in sorted(districts)])

        muhtar = json.loads((SOURCE / 'ibb_muhtarlik.geojson').read_text(encoding='utf-8'))['features']
        mah_seen = set()
        mah_rows = []
        for feat in muhtar:
            if not feat.get('geometry'):
                continue
            lon, lat = feat['geometry']['coordinates'][:2]
            if not point_in_Rings(lon, lat, ist_rings):
                continue
            p = feat['properties']
            ilce = tr_title(p.get('İlçe Adı') or '')
            mah = tr_title(p.get('Mahalle Adı') or '')
            if mah and (mah, ilce) not in mah_seen:
                mah_seen.add((mah, ilce))
                mah_rows.append((mah, 'neighbourhood (mahalle)', f'{ilce}, İstanbul' if ilce else 'İstanbul', lat, lon, 2, 'İBB Open Data'))
        add('mahalle', mah_rows)

        addr_db = sqlite3.connect(f"file:{GEO / 'addresses' / (REGION + '.sqlite')}?mode=ro", uri=True)
        try:
            postcodes = {}
            for code, city, lat, lon in addr_db.execute("SELECT zip, city, lat, lon FROM addr WHERE zip <> ''"):
                postcodes.setdefault(code, []).append((lat, lon, city))
        finally:
            addr_db.close()
        pc_rows = []
        for code, pts in sorted(postcodes.items()):
            pts.sort()
            lat, lon, city = pts[len(pts) // 2]
            pc_rows.append((code, 'postcode area', f'{city}, İstanbul' if city else 'İstanbul', lat, lon, 2, 'OpenStreetMap'))
        add('postcode', pc_rows)

        national = sqlite3.connect(f"file:{GEO / 'national.sqlite'}?mode=ro", uri=True)
        try:
            streets = {}
            for name, geom in national.execute("SELECT name, geom FROM feat WHERE layer='road' AND name IS NOT NULL AND name <> ''"):
                _, _, x, y = struct.unpack_from('<iiii', geom)
                lon, lat = x / 1e6, y / 1e6
                if point_in_Rings(lon, lat, ist_rings):
                    streets.setdefault(name, []).append((lat, lon))
            street_rows = []
            for name, pts in sorted(streets.items()):
                pts.sort()
                lat, lon = pts[len(pts) // 2]
                dist = district_for(lon, lat, districts)
                street_rows.append((name, 'street', f'{dist}, İstanbul' if dist else 'İstanbul', lat, lon, 3, 'OpenStreetMap'))
            add('street', street_rows)

            poi_rows = []
            for cls, name, lon, lat in national.execute("SELECT cls, name, lon, lat FROM poi WHERE name IS NOT NULL AND name <> ''"):
                dist = district_for(lon, lat, districts)
                poi_rows.append((name, cls.replace('_', ' '), f'{dist}, İstanbul' if dist else 'İstanbul', lat, lon, 4, 'İBB / OpenStreetMap'))
            add('poi', poi_rows)

            place_rows = []
            for cls, name, geom in national.execute("SELECT cls, name, geom FROM feat WHERE layer='place' AND name IS NOT NULL AND name <> ''"):
                _, _, x, y = struct.unpack_from('<iiii', geom)
                lon, lat = x / 1e6, y / 1e6
                if point_in_Rings(lon, lat, ist_rings):
                    place_rows.append((name, cls, 'İstanbul (OpenStreetMap)', lat, lon, 2, 'OpenStreetMap'))
            add('osm_place', place_rows)
        finally:
            national.close()

        db.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
        db.commit()
        db.execute('VACUUM')
    finally:
        db.close()
    return {'places': counts}


def build():
    fetched = json.loads((SOURCE / 'fetched.json').read_text(encoding='utf-8'))
    if DATA.exists():
        shutil.rmtree(DATA)
    GEO.mkdir(parents=True)
    started = time.time()
    ist_rings, districts = load_admin()
    counts = {}
    log('map (OpenStreetMap + İBB Official Emergency POIs)')
    counts.update(build_map_db(fetched, ist_rings))
    log('addresses')
    counts.update(build_addresses(fetched, ist_rings, districts))
    log('places')
    counts.update(build_places(ist_rings, districts))

    map_files = {f.relative_to(DATA).as_posix(): f.stat().st_size for f in sorted(GEO.glob('*.sqlite'))}
    (DATA / 'map-set.json').write_text(
        json.dumps({'name': REGION, 'files': map_files, 'bytes': sum(map_files.values()),
                    'seconds': round(time.time() - started)}, indent=1),
        encoding='utf-8',
    )
    outputs = {}
    for f in sorted(DATA.rglob('*')):
        if f.is_file():
            outputs[f.relative_to(DATA).as_posix()] = {'bytes': f.stat().st_size, 'sha256': sha256(f)}
    report = {
        'region': REGION, 'built': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'bbox': LAND_BBOX, 'clip_bbox': CLIP_BBOX,
        'sources': fetched, 'counts': counts, 'outputs': outputs,
        'note': 'map-set.json lists the map files; the address index is a separate sidecar, as for US states.',
    }
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding='utf-8')
    log('built in', round(time.time() - started), 's;', json.dumps(counts, ensure_ascii=False))


# ------------------------------------------------------------------ check ----

def check():
    from engine.address_search import find_addresses
    import server
    failures = []

    def expect(ok, what):
        print(('ok    ' if ok else 'FAIL  ') + what)
        if not ok:
            failures.append(what)

    report = json.loads(REPORT.read_text(encoding='utf-8'))
    for rel, info in report['outputs'].items():
        f = DATA / rel
        expect(f.is_file() and f.stat().st_size == info['bytes'] and sha256(f) == info['sha256'],
               f'{rel}: size and SHA-256 as built')
    map_set = json.loads((DATA / 'map-set.json').read_text(encoding='utf-8'))
    expect(map_set['name'] == REGION, f'map-set.json name is {REGION}')
    expect(all((DATA / rel).stat().st_size == size for rel, size in map_set['files'].items()),
           'map-set.json sizes match the files')
    for rel in [*map_set['files'], f'derived/geo/addresses/{REGION}.sqlite']:
        db = sqlite3.connect(f'file:{DATA / rel}?mode=ro', uri=True)
        expect(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', f'{rel}: SQLite integrity')
        db.close()

    west, south, east, north = CLIP_BBOX
    nat = sqlite3.connect(f"file:{GEO / 'national.sqlite'}?mode=ro", uri=True)
    roads = nat.execute("SELECT count(*) FROM feat WHERE layer='road'").fetchone()[0]
    bldgs = nat.execute("SELECT count(*) FROM feat WHERE layer='bldg'").fetchone()[0]
    expect(roads > 100000, f'streets drawn from OpenStreetMap: {roads:,} road features')
    expect(bldgs > 100000, f'building outlines in Istanbul: {bldgs:,}')
    for cls, low in (('hospital', 400), ('pharmacy', 5000), ('fire_station', 120), ('police', 150),
                     ('clinic', 1000), ('dentist', 2500), ('assembly_point', 100), ('drinking_water', 200)):
        n = nat.execute('SELECT count(*) FROM poi WHERE cls=?', (cls,)).fetchone()[0]
        expect(n >= low, f'help points {cls}: {n:,} (expected at least {low:,})')
    outside = nat.execute('SELECT count(*) FROM poi WHERE lon NOT BETWEEN ? AND ? OR lat NOT BETWEEN ? AND ?',
                          (west, east, south, north)).fetchone()[0]
    expect(outside == 0, f'every help point within Istanbul clip bounds ({outside} outside)')
    nat.close()

    addr = sqlite3.connect(f"file:{GEO / 'addresses' / (REGION + '.sqlite')}?mode=ro", uri=True)
    n, bad_zip, outside_a = addr.execute(
        "SELECT count(*), sum(zip <> '' AND zip NOT GLOB '[0-9][0-9][0-9][0-9][0-9]'),"
        " sum(lon NOT BETWEEN ? AND ? OR lat NOT BETWEEN ? AND ?) FROM addr",
        (LAND_BBOX[0], LAND_BBOX[2], LAND_BBOX[1], LAND_BBOX[3]),
    ).fetchone()
    expect(n > 25000, f'addresses in Istanbul: {n:,}')
    expect(not bad_zip, f'every present postcode is five digits ({bad_zip} malformed)')
    expect(not outside_a, f'every address inside Istanbul province bounds ({outside_a} outside)')
    samples = addr.execute(
        "SELECT street, num, city FROM addr WHERE zip LIKE '34%' AND length(street) > 8 LIMIT 3"
    ).fetchall()
    addr.close()

    for street, num, city in samples:
        q = f'{street} No: {num} {city}'
        found = find_addresses(q, GEO)
        expect(bool(found) and found[0]['name'] == f'{street} No: {num}',
               f'address search "{q}" -> {found[0]["name"] if found else "nothing"}')
    expect(find_addresses('Bağdat Caddesi No: 99999 Kadıköy', GEO) == [],
           'an unknown house number returns nothing, never a guess')

    places = sqlite3.connect(f"file:{GEO / 'places.sqlite'}?mode=ro", uri=True)
    for name, kind in (('Kadıköy', 'district (ilçe)'), ('Caferağa', 'neighbourhood (mahalle)'), ('İstanbul', 'province (il)')):
        hit = places.execute(
            'SELECT count(*) FROM place_fts f JOIN place p ON p.id=f.rowid WHERE place_fts MATCH ? AND p.name=? AND p.kind=?',
            (f'"{name}"', name, kind),
        ).fetchone()[0]
        expect(hit > 0, f'place finder has {kind} "{name}"')
    places.close()

    # Check ASCII/Turkish place folding through server.places()
    old_here, old_placedb, old_water = server.HERE, server.PLACEDB, server.WATERDB
    try:
        server.HERE = DATA
        server.PLACEDB = GEO / 'places.sqlite'
        server.WATERDB = DATA / 'no-water.sqlite'
        hits = server.places('Kadikoy')
        expect(any(h['name'] == 'Kadıköy' for h in hits), 'server.places("Kadikoy") finds "Kadıköy" via i/ı folding')
    finally:
        server.HERE, server.PLACEDB, server.WATERDB = old_here, old_placedb, old_water

    if failures:
        raise SystemExit(f'{len(failures)} check(s) failed')
    print('all checks passed')


def demo():
    folder = BASE / 'demo'
    (folder / 'library-data').mkdir(parents=True, exist_ok=True)
    (folder / 'library-data' / 'catalog.json').write_text('[]\n', encoding='utf-8')
    link = folder / 'derived'
    if not link.exists():
        try:
            link.symlink_to(Path('..') / 'data' / 'derived', target_is_directory=True)
        except OSError:
            shutil.copytree(DATA / 'derived', link)
    print(f'python3 server.py --data {folder.relative_to(ROOT)} --no-index')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('step', choices=('fetch', 'build', 'check', 'demo'))
    step = parser.parse_args().step
    {'fetch': fetch, 'build': build, 'check': check, 'demo': demo}[step]()


if __name__ == '__main__':
    main()
