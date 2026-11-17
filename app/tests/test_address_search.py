"""Offline address lookup must preserve house numbers and geographic qualifiers."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.address_search import (find_addresses, fold_tr, is_address_query, is_turkish_address_query,
                                   is_turkish_postcode_query, normalize, normalize_tr)


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
                           [(num, street, normalize_tr(street), city, 'TR-34', z, lat, lon, mahalle)
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
        self.assertEqual(fold_tr('İSTANBUL IĞDIR ŞİŞLİ KADIKÖY'), 'istanbul igdir sisli kadikoy')
        self.assertEqual(normalize_tr('İstiklâl Caddesi'), 'istiklal cd')
        self.assertEqual(normalize_tr('ISTIKLAL CAD.'), 'istiklal cd')
        self.assertEqual(normalize_tr('Veysi Paşa Sokağı'), 'veysi pasa sk')
        self.assertEqual(normalize_tr('Atatürk Bulvarı'), 'ataturk blv')
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
                self.assertFalse(is_turkish_address_query(query))
                self.assertEqual(find_addresses(query, self.geo), [])
        self.assertTrue(is_address_query('34710 Kadıköy'))
        self.assertTrue(is_turkish_postcode_query('34710 Kadıköy', self.geo))
        self.assertTrue(is_turkish_postcode_query('34710, Caferaga', self.geo))
        self.assertFalse(is_turkish_postcode_query('18003 nine mile', self.geo))
        self.assertFalse(is_turkish_postcode_query('34710 Besiktas', self.geo))

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
            self.assertEqual(server.places('18003 nine mile'), [])
            hit, = server.places('Bağdat Caddesi No: 75')
            self.assertEqual((hit['name'], hit['kind']), ('Bağdat Caddesi No: 75', 'Street address'))

if __name__ == '__main__':
    unittest.main()
