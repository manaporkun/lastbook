"""Offline address lookup must preserve house numbers and geographic qualifiers."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.address_search import (Scheme, find_addresses, fold_as, is_address_query,
                                   is_postcode_place_query, is_street_first_address_query,
                                   normalize, normalize_as)


class AddressSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_state('MI', [
            ('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47),
            ('18002', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.46, -83.47),
            ('18001', 'Nine Mile Rd', 'Farmington', 'MI', '48335', 42.47, -83.40),
        ])
        self.write_state('CA', [
            ('18001', 'Nine Mile Road', 'Example', 'CA', '90001', 34.0, -118.0),
            ('23001', 'Maple Avenue', 'Kansas City', 'MO', '64105', 39.0997, -94.5786),
            ('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01),
        ])

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, state, rows):
        db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute('CREATE INDEX addr_num_norm ON addr(num,norm)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)',
                           [(num, street, normalize(street), city, st, z, lat, lon)
                            for num, street, city, st, z, lat, lon in rows])
            db.commit()
        finally:
            db.close()      # the `with` form commits but does not close, and Windows will not remove an open file

    def test_numeric_words_directions_and_qualifiers(self):
        for query in ('18001 9 mile rd Novi MI', '18001 Nine Mile Road Novi Michigan',
                      '18001 nine mile Novi 48375', '18001 W Nine Mile Road, Novi, MI 48375'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '18001 W Nine Mile Road')
                self.assertEqual(result[0]['lat'], 42.45)
                self.assertEqual(result[0]['kind'], 'Street address')

    def test_unknown_number_does_not_return_nearby_house(self):
        self.assertEqual(find_addresses('18003 nine mile Novi MI', self.geo), [])

    def test_city_and_zip_do_not_silently_fall_back(self):
        self.assertEqual(find_addresses('18001 nine mile Wrongtown MI', self.geo), [])
        self.assertEqual(find_addresses('18001 nine mile MI 99999', self.geo), [])

    def test_zip_plus_four_uses_its_five_digit_zip(self):
        for query in ('18001 nine mile Novi MI 48375-1234', '18001 nine mile Novi MI 48375 1234'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '18001 W Nine Mile Road')
        self.assertEqual(find_addresses('18001 nine mile Novi MI 99999-1234', self.geo), [])

    def test_state_uses_only_that_index(self):
        result = find_addresses('18001 nine mile CA', self.geo)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['detail'], 'Example, CA, 90001')

    def test_ordinal_street(self):
        result = find_addresses('21 ninth st Example CA', self.geo)
        self.assertEqual(result[0]['name'], '21 W 9 ST')

    def test_plain_zip_remains_place_query(self):
        self.assertFalse(is_address_query('48375'))
        self.assertFalse(is_address_query('Novi MI'))
        self.assertTrue(is_address_query('18001 nine mile'))

    def test_missing_indexes_and_legacy_database(self):
        empty = self.geo / 'empty'
        self.assertEqual(find_addresses('23001 Maple Ave', empty), [])
        empty.mkdir()
        db = sqlite3.connect(empty / 'addresses.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute("INSERT INTO addr VALUES('23001','Maple Avenue','maple ave','Kansas City','MO','64105',39.0997,-94.5786)")
            db.commit()
        finally:
            db.close()      # as in write_state: the `with` form does not close, and Windows will not remove an open file
        self.assertEqual(find_addresses('23001 Maple Ave Kansas City MO', empty)[0]['lat'], 39.0997)

    def test_app_route_preserves_address_number(self):
        import server
        with patch.object(server, 'HERE', self.geo.parent), patch.object(server.address_search, 'find_addresses', return_value=[]) as lookup:
            self.assertEqual(server.places('18003 nine mile'), [])
            lookup.assert_called_once_with('18003 nine mile', self.geo.parent / 'derived' / 'geo', limit=8, near=None)


ISTANBUL_SOURCE = 'OpenStreetMap contributors, via Geofabrik turkey-latest-free.gpkg.zip (ODbL 1.0)'
ISTANBUL = [
    ('75', 'Bağdat Caddesi', '34724', 'Kadıköy', 'Feneryolu', 40.9806, 29.0427),
    ('18', 'Kadife Sokak', '34710', 'Kadıköy', 'Caferağa', 40.9866, 29.0265),
    ('7B', 'Abdullahağa Caddesi', '34676', 'Üsküdar', 'Beylerbeyi', 41.0435, 29.0439),
    ('135', '19 Mayıs Caddesi', '34360', 'Şişli', 'Meşrutiyet', 41.0541, 28.9897),
    ('41', 'İstiklâl Caddesi', '34435', 'Beyoğlu', 'Kuloğlu', 41.0335, 28.9778),
    ('10', 'Atatürk Bulvarı', '34134', 'Fatih', 'Aksaray', 41.0121, 28.9554),
    ('10', 'Atatürk Bulvarı', '34903', 'Pendik', 'Yenişehir', 40.9234, 29.3081),
]


class TurkishAddressSearchTests(unittest.TestCase):
    """Istanbul addresses are written street first, optional 'No:' and door number after, with district and mahalle."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_istanbul(self.geo / 'addresses' / 'TR-34.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_istanbul(path, rows=ISTANBUL, source=ISTANBUL_SOURCE):
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,district TEXT)')
            db.execute('CREATE INDEX addr_norm ON addr(norm,num)')
            db.execute('CREATE INDEX addr_zip ON addr(zip)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?,?)',
                           [(num, street, normalize_as('tr-1', street), city, 'TR-34', z, lat, lon, mahalle)
                            for num, street, z, city, mahalle, lat, lon in rows])
            db.executemany('INSERT INTO meta VALUES(?,?)', [('norm', 'tr-1'), ('source', source)])
            db.commit()
        finally:
            db.close()

    def write_us_state(self, state, rows, source=None):
        AddressSearchTests.write_state(self, state, rows)
        if source:
            db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
            try:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO meta VALUES('source',?)", (source,))
                db.commit()
            finally:
                db.close()

    def one(self, query):
        result = find_addresses(query, self.geo)
        self.assertEqual(len(result), 1, query)
        return result[0]

    def test_turkish_folding_and_street_abbreviations(self):
        """Dotted/dotless İ/I/ı, circumflex â, ş/ğ/ü/ö/ç and Cad./Sk./Blv. must fold identically."""
        self.assertEqual(fold_as('tr-1', 'İSTANBUL IĞDIR ŞİŞLİ KADIKÖY'), 'istanbul igdir sisli kadikoy')
        self.assertEqual(normalize_as('tr-1', 'İstiklâl Caddesi'), 'istiklal cd')
        self.assertEqual(normalize_as('tr-1', 'ISTIKLAL CAD.'), 'istiklal cd')
        self.assertEqual(normalize_as('tr-1', 'Veysi Paşa Sokağı'), 'veysi pasa sk')
        self.assertEqual(normalize_as('tr-1', 'Atatürk Bulvarı'), 'ataturk blv')
        self.assertEqual(normalize('18001 W Nine Mile Road'), '18001 w nine mile rd')

    def test_street_with_or_without_no_and_diacritics(self):
        for query in ('Bağdat Caddesi No: 75', 'Bagdat Cad. 75, 34724 Kadikoy', 'BAĞDAT CD NO 75 FENERYOLU MAH. KADIKÖY',
                      'Bagdat 75 Kadikoy'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['name'], 'Bağdat Caddesi No: 75')
                self.assertEqual(hit['detail'], 'Feneryolu, Kadıköy, 34724')
                self.assertEqual(hit['source'], ISTANBUL_SOURCE)

    def test_slash_and_letter_door_suffixes(self):
        for query in ('Abdullahağa Caddesi No: 7/B Üsküdar', 'Abdullahaga Cad 7B', 'Abdullahağa Cd. 7 b'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['name'], 'Abdullahağa Caddesi No: 7B')
        self.assertEqual(find_addresses('Bağdat Caddesi No: 75/1 Kadıköy', self.geo), [])

    def test_number_inside_street_name_kept_in_street(self):
        hit = self.one('19 Mayıs Caddesi No: 135 Şişli')
        self.assertEqual(hit['name'], '19 Mayıs Caddesi No: 135')
        self.assertEqual(self.one('19 Mayis Cad 135')['lat'], 41.0541)
        self.assertEqual(find_addresses('19 Mayıs Caddesi', self.geo), [])

    def test_district_and_mahalle_qualifiers_disambiguate(self):
        self.assertEqual(self.one('Atatürk Bulvarı 10 Fatih')['detail'], 'Aksaray, Fatih, 34134')
        self.assertEqual(self.one('Ataturk Blv No:10 Pendik')['detail'], 'Yenişehir, Pendik, 34903')
        self.assertEqual(self.one('Ataturk Bulvari 10 Yenisehir')['detail'], 'Yenişehir, Pendik, 34903')
        self.assertEqual(len(find_addresses('Atatürk Bulvarı 10 İstanbul', self.geo)), 2)
        self.assertEqual(find_addresses('Bağdat Caddesi 75 Beşiktaş', self.geo), [])
        self.assertEqual(find_addresses('Bağdat Caddesi 76 Kadıköy', self.geo), [])

    def test_postcode_and_district_stay_place_queries(self):
        for query in ('34710 Kadıköy', 'Kadıköy', 'İstanbul', '34710'):
            with self.subTest(query=query):
                self.assertFalse(is_street_first_address_query(query))
                self.assertEqual(find_addresses(query, self.geo), [])
        self.assertTrue(is_address_query('34710 Kadıköy'))
        self.assertTrue(is_postcode_place_query('34710 Kadıköy', self.geo))
        self.assertTrue(is_postcode_place_query('34710, Caferaga', self.geo))
        self.assertFalse(is_postcode_place_query('18003 nine mile', self.geo))
        self.assertFalse(is_postcode_place_query('34710 Besiktas', self.geo))

    def test_us_results_unchanged_with_istanbul_installed(self):
        (self.geo / 'addresses' / 'TR-34.sqlite').unlink()
        self.write_us_state('MI', [('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47)],
                            source='Overture Maps addresses 2026-10-21.0')
        self.write_us_state('CA', [('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01)])
        queries = ('18001 nine mile', '18001 W Nine Mile Road, Novi, MI 48375', '21 ninth st Example CA', '18003 nine mile')
        before = [find_addresses(query, self.geo) for query in queries]
        self.write_istanbul(self.geo / 'addresses' / 'TR-34.sqlite')
        self.assertEqual([find_addresses(query, self.geo) for query in queries], before)

    def test_app_route_falls_back_to_places_and_folds_turkish_place_queries(self):
        import server
        root = self.geo / 'root'
        geo = root / 'derived' / 'geo'
        (geo / 'addresses').mkdir(parents=True)
        self.write_istanbul(geo / 'addresses' / 'TR-34.sqlite')
        places = sqlite3.connect(geo / 'places.sqlite')
        try:
            places.executescript(
                "CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);"
                "CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Route 66','Locale','Arizona',35.2,-113.9,3,'gnis');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Kadıköy','district (ilçe)','İstanbul',40.99,29.03,1,'ibb');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('İstiklâl Caddesi','street','Beyoğlu, İstanbul',41.03,28.97,3,'osm');"
                "INSERT INTO place_fts(place_fts) VALUES('rebuild');")
            places.commit()
        finally:
            places.close()
        with patch.object(server, 'HERE', root), patch.object(server, 'PLACEDB', geo / 'places.sqlite'), \
                patch.object(server, 'WATERDB', root / 'no-water.sqlite'), patch.object(server, 'home_area', return_value=None):
            self.assertEqual([hit['name'] for hit in server.places('Route 66')], ['Route 66'])
            self.assertEqual([hit['name'] for hit in server.places('34710 Kadıköy')], ['Kadıköy'])
            self.assertEqual([hit['name'] for hit in server.places('Kadikoy')], ['Kadıköy'])
            self.assertEqual([hit['name'] for hit in server.places('ISTIKLAL CADDESI')], ['İstiklâl Caddesi'])
            (geo / 'addresses' / 'TR-34.sqlite').unlink()
            nat = sqlite3.connect(geo / 'national.sqlite')
            try:
                nat.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
                nat.execute("INSERT INTO meta VALUES ('region', 'TR-34')")
                nat.commit()
            finally:
                nat.close()
            self.assertEqual([hit['name'] for hit in server.places('Kadikoy')], ['Kadıköy'])
            self.write_istanbul(geo / 'addresses' / 'TR-34.sqlite')
            self.assertEqual(server.places('18003 nine mile'), [])
            hit, = server.places('Bağdat Caddesi No: 75')
            self.assertEqual((hit['name'], hit['kind']), ('Bağdat Caddesi No: 75', 'Street address'))


class GermanAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name) / 'root' / 'derived' / 'geo'
        (self.geo / 'addresses').mkdir(parents=True)
        db = sqlite3.connect(self.geo / 'addresses' / 'DE-BW-ST.sqlite')
        try:
            db.executescript(
                'CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,district TEXT);'
                "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);")
            rows = [
                ('5', 'Hauptstraße', 'Stuttgart', '70173', 48.7758, 9.1829, 'Stuttgart-Mitte'),
                ('28', 'Königstraße', 'Stuttgart', '70173', 48.7784, 9.1800, 'Stuttgart-Mitte'),
                ('12A', 'Berliner Straße', 'Stuttgart', '70376', 48.8156, 9.2275, 'Bad Cannstatt'),
                ('5', 'Straße des 17. Juni', 'Stuttgart', '70178', 48.7770, 9.1740, 'Stuttgart-West'),
                ('7', 'Calwer Straße', 'Stuttgart', '', 48.7740, 9.1775, 'Stuttgart-Mitte'),
                ('5', '19 Mai Straße', 'Stuttgart', '70173', 48.7700, 9.1700, 'Stuttgart-Mitte'),
                ('5', 'Am 17. Juni', 'Stuttgart', '70178', 48.7760, 9.1730, 'Stuttgart-West'),
                ('5', 'Calwer Straße', 'Stuttgart', '70173', 48.7741, 9.1776, 'Stuttgart-Mitte'),
                ('5', 'Calwer Straße', 'Stuttgart', '', 48.8000, 9.2200, 'Bad Cannstatt'),
                ('9', 'Marktstraße', 'Stuttgart', '70173', 48.8010, 9.2210, 'Bad Cannstatt'),
                ('51', 'Abelsbergstraße', 'Stuttgart', '70190', 48.7860, 9.2050, 'Stuttgart-Ost'),
                ('128', 'Mercedes-Benz Werk 1 (Untertürkheim)', 'Stuttgart', '70327', 48.7840, 9.2480, 'Untertürkheim'),
            ]
            db.executemany('INSERT INTO addr VALUES (?,?,?,?,?,?,?,?,?)',
                           [(num, street, normalize_as('de-1', street), city, 'DE-BW-ST', zip_code, lat, lon, district)
                            for num, street, city, zip_code, lat, lon, district in rows])
            db.execute("INSERT INTO meta VALUES ('norm','de-1')")
            db.execute("INSERT INTO meta VALUES ('source','OpenStreetMap (ODbL 1.0)')")
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_street_first_order_and_umlaut_spellings(self):
        for query in ('Königstraße 28', 'Koenigstrasse 28', 'Königstr. 28 Stuttgart', 'Königstraße 28, 70173 Stuttgart'):
            with self.subTest(query=query):
                found = find_addresses(query, self.geo)
                self.assertEqual([hit['name'] for hit in found], ['Königstraße 28'])
                self.assertEqual(found[0]['lat'], 48.7784)

    def test_letter_and_range_house_numbers(self):
        for query in ('Berliner Straße 12a', 'Berliner Str. 12 A'):
            with self.subTest(query=query):
                self.assertEqual([hit['name'] for hit in find_addresses(query, self.geo)],
                                 ['Berliner Straße 12A'])

    def test_range_house_numbers_use_the_first_endpoint(self):
        for query in ('Hauptstraße 5-7, 70173 Stuttgart', 'Hauptstraße 5-7a Stuttgart'):
            with self.subTest(query=query):
                hit, = find_addresses(query, self.geo)
                self.assertEqual(hit['name'], 'Hauptstraße 5')

    def test_postcode_does_not_hide_a_house_whose_own_postcode_is_missing(self):
        hit, = find_addresses('Calwer Straße 7, 70173 Stuttgart', self.geo)
        self.assertEqual(hit['name'], 'Calwer Straße 7')
        self.assertEqual(find_addresses('Calwer Straße 7, 99999 Stuttgart', self.geo), [])

    def test_one_letter_street_head_is_not_searched_as_a_prefix(self):
        self.assertEqual(find_addresses('B 10 5', self.geo), [])
        self.assertFalse(is_street_first_address_query('B 10 5'))

    def test_street_name_with_number_is_not_the_house(self):
        hit, = find_addresses('Straße des 17. Juni 5', self.geo)
        self.assertEqual(hit['name'], 'Straße des 17. Juni 5')

    def test_unknown_house_or_city_returns_nothing(self):
        self.assertEqual(find_addresses('Hauptstraße 999 Stuttgart', self.geo), [])
        self.assertEqual(find_addresses('Königstraße 28, 80331 München', self.geo), [])

    def test_postcode_and_city_are_place_queries(self):
        self.assertFalse(is_street_first_address_query('70173 Stuttgart'))
        self.assertFalse(is_street_first_address_query('Stuttgart'))
        self.assertTrue(is_street_first_address_query('Hauptstraße 5'))

    def test_street_name_starting_with_a_number_is_reachable(self):
        hit, = find_addresses('19 Mai Straße 5', self.geo)
        self.assertEqual(hit['name'], '19 Mai Straße 5')

    def test_number_first_spelling_does_not_borrow_the_street_first_index(self):
        self.assertEqual(find_addresses('28 Koenigstrasse', self.geo), [])

    def test_qualifier_decides_before_postcode_preference(self):
        hit, = find_addresses('Calwer Straße 5, 70173 Bad Cannstatt', self.geo)
        self.assertEqual(hit['lat'], 48.8000)

    def test_later_number_candidate_is_tried_when_the_first_fails(self):
        hit, = find_addresses('Am 17. Juni 5', self.geo)
        self.assertEqual(hit['name'], 'Am 17. Juni 5')
        hit, = find_addresses('Mercedes-Benz Werk 1 (Untertürkheim) 128', self.geo)
        self.assertEqual(hit['name'], 'Mercedes-Benz Werk 1 (Untertürkheim) 128')

    def test_numeric_slash_suffix_does_not_match_the_base_house(self):
        self.assertEqual(find_addresses('Abelsbergstraße 51/1', self.geo), [])
        self.assertEqual(find_addresses('Abelsbergstraße 51 / 1 Stuttgart', self.geo), [])
        hit, = find_addresses('Abelsbergstraße 51', self.geo)
        self.assertEqual(hit['name'], 'Abelsbergstraße 51')

    def test_index_without_a_norm_meta_does_not_break_street_first_search(self):
        db = sqlite3.connect(self.geo / 'addresses' / 'CA.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,district TEXT)')
            db.commit()
        finally:
            db.close()
        hit, = find_addresses('Königstraße 28', self.geo)
        self.assertEqual(hit['name'], 'Königstraße 28')

    def test_region_code_must_be_iso_shaped_text(self):
        import server
        national = self.geo / 'national.sqlite'
        cases = (('DE-STG', 'DE-STG'), ('TR-34', 'TR-34'), ('DE', None), ('CA', None),
                 ('ca-on', None), ('US', None), ('', None), (b'DE-STG', None), (12345, None))
        with patch.object(server, 'HERE', self.geo.parents[1]):
            for value, want in cases:
                national.unlink(missing_ok=True)
                db = sqlite3.connect(national)
                try:
                    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value)')
                    db.execute("INSERT INTO meta VALUES ('region',?)", (value,))
                    db.commit()
                finally:
                    db.close()
                with self.subTest(value=value):
                    self.assertEqual(server.region_of_national(), want)

    def test_app_route_reaches_the_german_index(self):
        import server
        with patch.object(server, 'HERE', self.geo.parents[1]), \
                patch.object(server, 'PLACEDB', self.geo / 'no-places.sqlite'), \
                patch.object(server, 'WATERDB', self.geo / 'no-water.sqlite'), \
                patch.object(server, 'home_area', return_value=None):
            hit, = server.places('Hauptstraße 5')
            self.assertEqual((hit['name'], hit['kind']), ('Hauptstraße 5', 'Street address'))


class SchemeDataTests(unittest.TestCase):
    """A second street-first country is data in a Scheme, not new engine code."""

    def setUp(self):
        self.scheme = Scheme(
            chars={'İ': 'i', 'I': 'i', 'ı': 'i', 'Ğ': 'g', 'ğ': 'g', 'Ü': 'u', 'ü': 'u',
                   'Ş': 's', 'ş': 's', 'Ö': 'o', 'ö': 'o', 'Ç': 'c', 'ç': 'c',
                   'Â': 'a', 'â': 'a', 'Î': 'i', 'î': 'i', 'Û': 'u', 'û': 'u'},
            words={'caddesi': 'cd', 'sokagi': 'sk'},
            markers=('no',),
            number_pick='last',
            street_kinds=('cd', 'sk'),
            form_suffixes=('cd', 'sk'),
            place_stop=('istanbul',),
            name='{street} No: {num}',
        )

    def test_marker_and_folded_letters(self):
        number, street, postcode, qualifier = self.scheme.parse('Süleyman Seba Caddesi No: 22 Beşiktaş')
        self.assertEqual((number, street, postcode, qualifier),
                         ('22', ['suleyman', 'seba', 'caddesi'], '', ['besiktas']))
        self.assertEqual(self.scheme.normalize('İstiklâl Caddesi'), 'istiklal cd')

    def test_unknown_scheme_modes_are_refused(self):
        with self.assertRaises(ValueError):
            Scheme(chars={}, words={}, postcode_policy='loose')
        with self.assertRaises(ValueError):
            Scheme(chars={}, words={}, number_pick='middle')

    def test_street_name_may_start_with_a_number(self):
        number, street, postcode, qualifier = self.scheme.parse('19 Mayıs Caddesi 135')
        self.assertEqual((number, street), ('135', ['19', 'mayis', 'caddesi']))


if __name__ == '__main__':
    unittest.main()
