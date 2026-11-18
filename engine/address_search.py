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


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


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


class Scheme:
    """One street-first address scheme, as data. An index declares its scheme in meta ('norm').

    The engine holds no code per country: adding a street-first country is one entry in SCHEMES
    saying how its letters fold, which street words shorten, what word marks the house number,
    what a postcode and a house number look like, which words after the number merely name the
    place, and how a found address is written back."""

    def __init__(self, chars, words, markers=(), number_pick='first', skip_after=(),
                 number=r'\d{1,4}[a-z]?', postcode=r'\d{5}', postcode_policy='strict',
                 street_kinds=(), form_suffixes=(), place_stop=(), name='{street} {num}',
                 min_street=3, fts_pairs=()):
        if number_pick not in ('first', 'last'):
            raise ValueError(f'unknown number_pick {number_pick!r}')
        if postcode_policy not in ('strict', 'district-loose'):
            raise ValueError(f'unknown postcode_policy {postcode_policy!r}')
        self.chars = str.maketrans(chars)
        self.words = words
        self.markers = markers
        self.number_pick = number_pick
        self.skip_after = skip_after
        self.number = re.compile(number)
        self.postcode = re.compile(postcode)
        self.postcode_policy = postcode_policy
        self.street_kinds = street_kinds
        self.form_suffixes = form_suffixes
        self.place_stop = place_stop
        self.name = name
        self.min_street = min_street
        self.fts_pairs = fts_pairs

    def fold(self, text):
        """Map the scheme's letters, lower-case, and drop any other accents."""
        text = unicodedata.normalize('NFC', text).translate(self.chars).lower()
        text = ''.join(ch for ch in unicodedata.normalize('NFD', text) if unicodedata.category(ch) != 'Mn')
        return text.translate(self.chars)

    def normalize(self, text):
        """'Königstraße' -> 'koenigstrasse', 'Berliner Straße' -> 'berliner str' under de-1."""
        # Suffixes fold as whole words; a compound keeps its letters and the range query matches by prefix.
        words = re.sub(r'[^a-z0-9 ]+', ' ', self.fold(text)).split()
        return ' '.join(self.words.get(word, word) for word in words)

    def tokens(self, query):
        text = self.fold(query)
        if self.markers:
            marker = '|'.join(re.escape(word) for word in self.markers)
            text = re.sub(rf'\b(?:{marker})\.?\s*[:.]?\s*(?=\d)', f' {self.markers[0]} ', text)
        text = re.sub(r'(\d+)\s*/\s*([a-z])\b', r'\1\2', text)               # '5/a' is one house number
        text = re.sub(r'(\d+)\s*/\s*(\d+[a-z]?)\b', r'\1/\2', text)          # '51/1' is a distinct parcel door, not a range
        text = re.sub(r'(\b\d+)\s*[-–]\s*\d+[a-z]?\b', r'\1', text)          # a range ('5-7') answers at its first end
        return re.sub(r'[^a-z0-9/ ]+', ' ', text).replace(' / ', ' ').split()

    def _candidates(self, tokens):
        """(house-number index, street-end index) pairs in preference order."""
        for index in range(len(tokens) - 2, 0, -1):                          # an explicit marker ('No: 22') wins
            if tokens[index] in self.markers and self.number.fullmatch(tokens[index + 1]):
                yield index + 1, index
        indexes = range(1, len(tokens)) if self.number_pick == 'first' else range(len(tokens) - 1, 0, -1)
        for index in indexes:
            token = tokens[index]
            if self.postcode.fullmatch(token):
                continue                                  # a postcode, not a house number
            if self.number.fullmatch(token) and tokens[index - 1] not in self.skip_after:
                yield index, index

    def parse_all(self, query):
        """All valid (number, street words, postcode, qualifier words) candidates in preference order."""
        tokens = self.tokens(query)
        if len(tokens) < 2:
            return
        seen = set()
        for number_index, street_end in self._candidates(tokens):
            parsed = self._parse_at(tokens, number_index, street_end)
            if parsed is not None:
                key = (parsed[0], tuple(parsed[1]), parsed[2], tuple(parsed[3]))
                if key not in seen:
                    seen.add(key)
                    yield parsed

    def parse(self, query):
        """(number, street words, postcode, qualifier words) in street-first order, or None."""
        return next(self.parse_all(query), None)

    def _parse_at(self, tokens, number_index, street_end):
        number = tokens[number_index].upper()
        street = tokens[:street_end]
        rest = list(tokens[number_index + 1:])
        if rest and re.fullmatch(r'[a-z]', rest[0]) and not number[-1].isalpha():
            number += rest.pop(0).upper()                  # '12 a' as written on many letters; '12a b' is not 12AB
        while rest and self.number.fullmatch(rest[0]) and not self.postcode.fullmatch(rest[0]):
            rest.pop(0)                                   # the second half of a spaced range ('5 7'), never a postcode
        postcode = ''
        for index, token in enumerate(rest):
            if self.postcode.fullmatch(token):
                postcode = token
                rest.pop(index)
                break
        qualifier = [token for token in rest if self.words.get(token, token) not in self.place_stop]
        if not street or len(' '.join(street)) < self.min_street or not any(re.search(r'[a-z]', word) for word in street):
            return None                                  # 'B 10 5' must not answer Bodoweg 10
        if any('/' in token for token in qualifier):
            return None                                  # an unindexed slash door ('51/1') must not fall back to another number in the street name
        if self.number.fullmatch(street[-1]):
            return None                                  # a street ending in a bare number is a road designation, not a name to search by prefix
        return number, street, postcode, qualifier

    def street_forms(self, words):
        mapped = [self.words.get(word, word) for word in words]
        base = ' '.join(mapped)
        forms = [base]
        if mapped and mapped[-1] not in self.street_kinds:
            forms += [f'{base} {suffix}' for suffix in self.form_suffixes]
        return list(dict.fromkeys(forms))

    def fts_variants(self, word):
        """Spellings of one word for the place finder's FTS index.

        SQLite unicode61 folds most accents but keeps some letters distinct (dotless ı is not i);
        fts_pairs names those letters, so an ASCII query still matches the stored spelling."""
        if not self.fts_pairs:
            return [word]
        base = self.fold(word)
        variants = [base]
        for folded, native in self.fts_pairs:
            positions = [index for index, char in enumerate(base) if char == folded][:3]
            for index in positions:
                variants += [item[:index] + native + item[index + 1:] for item in variants[:]]
        if word not in variants:
            variants.insert(0, word)
        return list(dict.fromkeys(variants))

    def place_matches(self, qualifier, city, district):
        words = [word for word in self.normalize(qualifier).split() if word not in self.place_stop]
        if not words:
            return True
        hay = ' '.join(part for part in (self.normalize(city or ''), self.normalize(district or '')) if part).split()
        squashed = ''.join(hay)                          # 'badcannstatt' as typed, 'Bad Cannstatt' as stored
        return all(any(h.startswith(word) for h in hay) or squashed.startswith(word) for word in words)


SCHEMES = {
    'de-1': Scheme(
        chars={'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue', 'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss'},
        words={'strasse': 'str', 'platz': 'pl', 'sankt': 'st', 'saint': 'st'},
        markers=('nr', 'nummer', 'hausnummer'),
        skip_after=('des',),
        postcode_policy='district-loose',
        street_kinds=('str', 'pl', 'weg', 'allee', 'gasse', 'damm', 'ring', 'steige'),
        form_suffixes=('str',),
        place_stop=('deutschland', 'de'),                # country only: a city name here would hide another city's houses
    ),
    'tr-1': Scheme(
        chars={'İ': 'i', 'I': 'i', 'ı': 'i', 'Ğ': 'g', 'ğ': 'g', 'Ü': 'u', 'ü': 'u',
               'Ş': 's', 'ş': 's', 'Ö': 'o', 'ö': 'o', 'Ç': 'c', 'ç': 'c',
               'Â': 'a', 'â': 'a', 'Î': 'i', 'î': 'i', 'Û': 'u', 'û': 'u'},
        words={'cadde': 'cd', 'caddesi': 'cd', 'cad': 'cd',
               'sokak': 'sk', 'sokagi': 'sk', 'sok': 'sk',
               'bulvar': 'blv', 'bulvari': 'blv', 'bulv': 'blv',
               'meydan': 'myd', 'meydani': 'myd', 'meyd': 'myd',
               'mahalle': 'mh', 'mahallesi': 'mh', 'mah': 'mh',
               'yolu': 'yol'},
        markers=('no',),
        number_pick='last',                              # street names may start with a number ('19 Mayıs Caddesi 135')
        number=r'\d+[a-z]?',
        street_kinds=('cd', 'sk', 'blv', 'myd'),
        form_suffixes=('cd', 'sk', 'blv'),
        place_stop=('istanbul', 'tr', 'mh'),
        name='{street} No: {num}',
        fts_pairs=(('i', 'ı'),),
    ),
}


def normalize_as(norm, text):
    """Normalize a street name the way the index that declares scheme `norm` stored it."""
    return SCHEMES[norm].normalize(text)


def fold_as(norm, text):
    """Fold letters the way scheme `norm` does, without its street-word shortening."""
    return SCHEMES[norm].fold(text)


def fold_any(text, geo_directory=None):
    """Fold letters for place-name comparison, using the installed locale when known."""
    schemes = [scheme for _, scheme in _installed_schemes(geo_directory)] if geo_directory else []
    if len(schemes) == 1:
        return schemes[0].fold(text)
    for scheme in reversed(schemes or list(SCHEMES.values())):
        text = scheme.fold(text)
    return text


_REGION_NORM = {'DE': 'de-1', 'TR': 'tr-1'}


def _installed_schemes(geo_directory):
    """(norm, scheme) pairs some installed index or region map declares, in registry order."""
    geo_directory = Path(geo_directory)
    installed = {_index_info(path)[0] for path in sorted((geo_directory / 'addresses').glob('*.sqlite'))}
    for name in ('places.sqlite', 'national.sqlite'):
        path = geo_directory / name
        if path.is_file():
            norm, _ = _index_info(path)
            if norm in SCHEMES:
                installed.add(norm)
    return [(norm, scheme) for norm, scheme in SCHEMES.items() if norm in installed]


def fts_variants(word, geo_directory):
    """Spellings to try for one place-finder word, from the installed indexes' schemes."""
    variants = [word]
    for _, scheme in _installed_schemes(geo_directory):
        variants += scheme.fts_variants(word)
    return list(dict.fromkeys(variants))


def is_postcode_place_query(query, geo_directory):
    """'70173 Stuttgart': a postcode and a place some installed street-first index knows together."""
    parts = query.strip().split(None, 1)
    if len(parts) < 2:
        return False
    first = parts[0].rstrip(',')
    rest = parts[1].lstrip(' ,')
    if not rest or any(ch.isdigit() for ch in rest):
        return False
    geo_directory = Path(geo_directory)
    for norm, scheme in _installed_schemes(geo_directory):
        if not scheme.postcode.fullmatch(first):
            continue
        qualifier = ' '.join(word for word in scheme.normalize(rest).split()
                             if word not in scheme.place_stop)
        if not qualifier:
            continue
        for path in sorted((geo_directory / 'addresses').glob('*.sqlite')):
            if _index_info(path)[0] != norm:
                continue
            try:
                connection = sqlite3.connect(ro_uri(path), uri=True)
                try:
                    has_district = any(row[1] == 'district' for row in connection.execute('PRAGMA table_info(addr)'))
                    district = 'district' if has_district else "''"
                    found = connection.execute(f'SELECT DISTINCT city, {district} FROM addr WHERE zip = ?',
                                               (first,)).fetchall()
                finally:
                    connection.close()
            except sqlite3.Error:
                continue
            if any(scheme.place_matches(qualifier, city, district) for city, district in found):
                return True
    return False


def is_street_first_address_query(query):
    """Street, then house number ('Hauptstraße 5, 70173 Stuttgart') under some known scheme."""
    return any(scheme.parse(query) is not None for scheme in SCHEMES.values())


def _postcode_fits(connection, postcode, district, city):
    """Whether this index uses that postcode for the district (or city) an address without one belongs to."""
    column, value = ('district', district) if district else ('city', city)
    if not value:
        return False
    try:
        return connection.execute(f'SELECT 1 FROM addr WHERE zip = ? AND {column} = ? LIMIT 1',
                                  (postcode, value)).fetchone() is not None
    except sqlite3.Error:
        return False


def _find_street_first(scheme, norm, parts, geo_directory, limit, near):
    number, words, postcode, qualifier = parts
    qual = ' '.join(qualifier)
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == norm]
    rows = []
    for path in paths:
        source = _index_info(path)[1] or 'OpenStreetMap'
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            has_district = any(row[1] == 'district' for row in connection.execute('PRAGMA table_info(addr)'))
            district = 'district' if has_district else "''"   # older indexes have no district column
            fits = {}
            for form in scheme.street_forms(words):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if postcode and scheme.postcode_policy == 'strict':
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                sql = (f'SELECT num,street,city,state,zip,lat,lon,norm,{district} FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT 500')
                found = connection.execute(sql, parameters).fetchall()
                if qual:
                    found = [row for row in found if scheme.place_matches(qual, row[2], row[8])]
                if postcode and scheme.postcode_policy == 'district-loose':
                    # A mapped house with no postcode of its own still answers a postcode this district really uses; a wrong one answers nothing.
                    def fits_here(row):
                        key = (row[8] or '', row[2] or '')
                        if key not in fits:
                            fits[key] = _postcode_fits(connection, postcode, *key)
                        return fits[key]
                    exact = [row for row in found if row[4] == postcode]
                    loose = [row for row in found if not row[4] and fits_here(row)]
                    found = exact or loose
                rows.extend((*row, source) for row in found)
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
            'name': scheme.name.format(street=name, num=num), 'kind': 'Street address',
            'detail': where,
            'lat': lat, 'lon': lon, 'source': source, 'onMap': True,
        })
        if len(results) == limit:
            break
    return results


_INFO = {}


def _index_info(path):
    """(norm scheme, source line) from an index's meta table, cached until the file changes."""
    try:
        stat = path.stat()
    except OSError:
        return US_NORM, None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _INFO.get(str(path))
    if cached is None or cached[0] != stamp:
        meta = {}
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                meta = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('norm','source','region')").fetchall())
            finally:
                connection.close()
        except sqlite3.Error:
            pass
        region = meta.get('region') or ''
        norm = meta.get('norm') or (_REGION_NORM.get(region.split('-', 1)[0]) if isinstance(region, str) else None) or US_NORM
        cached = (stamp, (norm, meta.get('source')))
        _INFO[str(path)] = cached
    return cached[1]


def _rank(near):
    def rank(row):
        distance = 0
        if near:
            distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return len(row[7]), distance, row[3], row[2], row[1]
    return rank


def _find_street_first_installed(query, geo_directory, limit, near):
    for norm, scheme in _installed_schemes(geo_directory):
        for parts in scheme.parse_all(query):
            found = _find_street_first(scheme, norm, parts, geo_directory, limit, near)
            if found:
                return found
    return []


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house.

    Number-first queries search US-normalized indexes first; a street name that itself starts
    with a number ('19 Mai Straße 5') then still reaches the installed street-first schemes."""
    geo_directory = Path(geo_directory)
    if is_address_query(query):
        found = _find_us(query, geo_directory, limit, near)
        if found:
            return found
    return _find_street_first_installed(query, geo_directory, limit, near)


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
                def rank(row):
                    distance = 0
                    if near:
                        distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
                    return len(row[7]), distance, row[3], row[2], row[1]
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=rank):
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
