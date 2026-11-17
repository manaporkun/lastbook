"""Read-only address lookup using the state indexes installed with offline maps."""
import math
import re
import sqlite3
import unicodedata
from pathlib import Path
from engine.paths import ro_uri

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}

US_NORM = 'us'        # indexes without a meta 'norm' key: the published US state indexes and the legacy addresses.sqlite
TR_NORM = 'tr-1'
_TR_CHARS = str.maketrans({
    'İ': 'i', 'I': 'i', 'ı': 'i',
    'Ğ': 'g', 'ğ': 'g',
    'Ü': 'u', 'ü': 'u',
    'Ş': 's', 'ş': 's',
    'Ö': 'o', 'ö': 'o',
    'Ç': 'c', 'ç': 'c',
    'Â': 'a', 'â': 'a', 'Î': 'i', 'î': 'i', 'Û': 'u', 'û': 'u',
})
TR_WORDS = {
    'cadde': 'cd', 'caddesi': 'cd', 'cad': 'cd', 'cd': 'cd',
    'sokak': 'sk', 'sokagi': 'sk', 'sok': 'sk', 'sk': 'sk',
    'bulvar': 'blv', 'bulvari': 'blv', 'bulv': 'blv', 'blv': 'blv',
    'meydan': 'myd', 'meydani': 'myd', 'meyd': 'myd', 'myd': 'myd',
    'mahalle': 'mh', 'mahallesi': 'mh', 'mah': 'mh', 'mh': 'mh',
    'yolu': 'yol',
}


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))



def fold_tr(text):
    """Fold Turkish letters (İ/I/ı -> i, ş -> s, ğ -> g, ü -> u, ö -> o, ç -> c) without leaving combining dots."""
    text = unicodedata.normalize('NFC', text).translate(_TR_CHARS).lower()
    text = ''.join(ch for ch in unicodedata.normalize('NFD', text) if unicodedata.category(ch) != 'Mn')
    return text.translate(_TR_CHARS)


def tr_fts_variants(word):
    """SQLite unicode61 folds ş/ğ/ü/ö/ç/İ/â to ASCII, but treats dotless ı as distinct from i.
    Generating i/ı variants (up to 3 positions) lets ASCII 'Kadikoy' match stored 'Kadıköy'."""
    base = fold_tr(word)
    positions = [idx for idx, ch in enumerate(base) if ch == 'i'][:3]
    if not positions:
        return [word]
    out = [base]
    for idx in positions:
        out += [item[:idx] + 'ı' + item[idx + 1:] for item in out[:]]
    if word not in out:
        out.insert(0, word)
    return list(dict.fromkeys(out))


def normalize_tr(text):
    """Scheme 'tr-1': 'İstiklâl Caddesi' -> 'istiklal cd', '19 Mayıs Sokak' -> '19 mayis sk'."""
    words = re.sub(r'[^a-z0-9 ]+', ' ', fold_tr(text)).split()
    return ' '.join(TR_WORDS.get(word, word) for word in words)


def is_turkish_address_query(query):
    """Street words, optional 'No', then a door number and optional postcode/district ('Bağdat Caddesi No: 75 Kadıköy')."""
    return _parts_tr(query) is not None


def _clean_tr_tokens(query):
    raw = fold_tr(query)
    raw = re.sub(r'\bno\s*[:.]?\s*(?=\d)', 'no ', raw)
    raw = re.sub(r'(\d+)\s*/\s*([a-z])\b', r'\1\2', raw)
    raw = re.sub(r'(\b\d+)\s*[-–]\s*\d+[a-z]?\b', r'\1', raw)
    return re.sub(r'[^a-z0-9 ]+', ' ', raw).split()


def _parts_tr(query):
    """(number, street words, postcode, qualifier words) in Turkish street-first order, or None."""
    tokens = [TR_WORDS.get(t, t) for t in _clean_tr_tokens(query)]
    if not tokens:
        return None
    postcode = ''
    for i in range(len(tokens) - 1, 0, -1):
        if re.fullmatch(r'\d{5}', tokens[i]):
            before = tokens[:i]
            if any(re.fullmatch(r'\d+[a-z]?', t) for t in before):
                postcode = tokens.pop(i)
                break
    no_idx = -1
    for i in range(len(tokens) - 1, 0, -1):
        if tokens[i] == 'no' and i + 1 < len(tokens) and re.fullmatch(r'\d+[a-z]?', tokens[i + 1]):
            no_idx = i
            break
    if no_idx >= 1:
        head = tokens[:no_idx]
        num_tok = tokens[no_idx + 1]
        rest = tokens[no_idx + 2:]
        suffix = ''
        if num_tok.isdigit() and rest and re.fullmatch(r'[a-z]', rest[0]):
            suffix = rest.pop(0)
        number = (num_tok + suffix).upper()
        qualifier = [t for t in rest if t not in ('mh', 'istanbul', 'tr')]
    else:
        end = len(tokens)
        while end and not re.search(r'\d', tokens[end - 1]):
            end -= 1
        head, rest = tokens[:end], tokens[end:]
        suffix = ''
        if head and head[-1].isdigit() and rest and re.fullmatch(r'[a-z]', rest[0]):
            suffix = rest.pop(0)
        elif len(head) >= 2 and re.fullmatch(r'[a-z]', head[-1]) and head[-2].isdigit():
            suffix = head.pop()
        if not head or not re.fullmatch(r'\d+[a-z]?', head[-1]):
            return None
        number = (head.pop() + suffix).upper()
        qualifier = [t for t in rest if t not in ('mh', 'istanbul', 'tr')]
    if not head or not any(re.search(r'[a-z]', w) for w in head):
        return None
    return number, head, postcode, qualifier


def is_turkish_postcode_query(query, geo_directory):
    """'34710 Kadıköy': a 5-digit postcode and place known to an installed Turkish index."""
    match = re.fullmatch(r'\s*(\d{5})[\s,]+([^\d]+)', query)
    if not match:
        return False
    postcode = match.group(1)
    qual = [w for w in normalize_tr(match.group(2)).split() if w not in ('mh',)]
    if not qual:
        return False
    qualifier = ' '.join(qual)
    for path in sorted((Path(geo_directory) / 'addresses').glob('*.sqlite')):
        if _index_info(path)[0] != TR_NORM:
            continue
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                found = connection.execute('SELECT DISTINCT city, district FROM addr WHERE zip = ?', (postcode,)).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        if any(_tr_place_matches(qualifier, city, district) for city, district in found):
            return True
    return False


def _tr_place_matches(qualifier, city, district):
    words = [w for w in qualifier.split() if w not in ('mh', 'istanbul', 'tr')]
    if not words:
        return True
    hay = ' '.join(part for part in (normalize_tr(city or ''), normalize_tr(district or '')) if part).split()
    return all(any(h.startswith(w) for h in hay) for w in words)


_INFO = {}


def _index_info(path):
    """(norm scheme, source line) from an index's meta table, cached until the file changes."""
    try:
        stat = path.stat()
    except OSError:
        return US_NORM, None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _INFO:
        meta = {}
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                meta = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('norm','source')").fetchall())
            finally:
                connection.close()
        except sqlite3.Error:
            pass
        _INFO[key] = (meta.get('norm') or US_NORM, meta.get('source'))
    return _INFO[key]

def street_forms(street):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    if words[0] not in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _parts(query):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    # A ZIP+4 ("62704-1234", or "62704 1234" once the hyphen is gone) is the same place as its five-digit ZIP, which the
    # index keeps: drop the four extra digits so the ZIP and the state before it are still read.
    if len(remainder) > 1 and re.fullmatch(r'\d{4}', remainder[-1]) and re.fullmatch(r'\d{5}', remainder[-2]):
        remainder.pop()
    postcode = ''
    if remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    state = ''
    for count in (3, 2, 1):
        if len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode


def _rank(near):
    def rank(row):
        distance = 0
        if near:
            distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return len(row[7]), distance, row[3], row[2], row[1]
    return rank


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house.

    Number-first queries search US-normalized indexes first; if none match and a tr-1 index is installed,
    streets starting with a date or ordinal ('19 Mayıs Caddesi No: 135') are searched with Turkish rules."""
    geo = Path(geo_directory)
    if is_address_query(query):
        found = _find_us(query, geo, limit, near)
        if found:
            return found
    parts = _parts_tr(query)
    if parts is None:
        return []
    return _find_tr(parts, geo, limit, near)

def _tr_street_forms(words):
    base = ' '.join(words)
    forms = [base]
    if words[-1] not in ('cd', 'sk', 'blv', 'myd'):
        for kind in ('cd', 'sk', 'blv'):
            forms.append(f'{base} {kind}')
    return forms


def _find_tr(parts, geo_directory, limit, near):
    number, words, postcode, qualifier = parts
    qual = ' '.join(qualifier)
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == TR_NORM]
    if not paths:
        return []
    rows = []
    for path in paths:
        source = _index_info(path)[1] or 'OpenStreetMap'
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            for form in _tr_street_forms(words):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm,district FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT 500')
                found = connection.execute(sql, parameters).fetchall()
                rows.extend((*row, source) for row in found if not qual or _tr_place_matches(qual, row[2], row[8]))
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    results, seen = [], []
    for num, name, city, state, zip_code, lat, lon, normalized, district, source in sorted(rows, key=_rank(near)):
        if any(n == num and s == normalized and c == city and abs(lat - y) < 0.0005 and abs(lon - x) < 0.0006
               for n, s, c, y, x in seen):
            continue
        seen.append((num, normalized, city, lat, lon))
        where = ', '.join(part for part in (district, city, zip_code) if part)
        results.append({
            'name': f'{name} No: {num}', 'kind': 'Street address',
            'detail': where or 'İstanbul',
            'lat': lat, 'lon': lon, 'source': source, 'onMap': True,
        })
        if len(results) == limit:
            break
    return results


def _find_us(query, geo_directory, limit, near):
    number, words, state, postcode = _parts(query)
    if not words:
        return []
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == US_NORM]
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append(sqlite3.connect(ro_uri(path), uri=True))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            for form in street_forms(street):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if state:
                    conditions.append('state = ?')
                    parameters.append(state)
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                if city:
                    conditions.append('lower(city) LIKE ?')
                    parameters.append(city + '%')
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT ?')
                for connection in connections:
                    try:
                        rows.extend(connection.execute(sql, [*parameters, limit * 3]).fetchall())
                    except sqlite3.Error:
                        continue
            if rows:
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=_rank(near)):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, state_code, zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection in connections:
            connection.close()
