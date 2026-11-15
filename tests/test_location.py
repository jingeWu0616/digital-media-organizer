import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pillow_heif import register_heif_opener
from import_service import ImportService, digest
from location_service import LocationService, photo_metadata, coordinates, protect_key, search_places
from media_catalog import Catalog
from processing_history import ProcessingHistory


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / '中文照片'; self.source.mkdir()
        self.catalog = Catalog(self.base / 'data/catalog.sqlite3'); self.catalog.initialize()
        self.service = LocationService(self.catalog)

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, suffix='.jpg'):
        path = self.source / ('原片' + suffix)
        exif = Image.Exif(); exif[271] = 'Test camera'; exif[272] = 'Camera A'
        exif[274] = 6; exif[306] = '2025:02:03 04:05:06'
        if suffix == '.heic': register_heif_opener()
        Image.new('RGB', (80, 60), '#245678').save(path, exif=exif, format='HEIF' if suffix == '.heic' else None)
        self.catalog.scan(self.source)
        return path, self.catalog.search(text=path.name)[1][0]

    def test_all_supported_formats_preserve_image_and_existing_exif(self):
        for suffix in ('.jpg', '.png', '.tiff', '.heic'):
            with self.subTest(suffix=suffix):
                path, row = self.photo(suffix)
                before, stamp = photo_metadata(path), path.stat().st_mtime_ns
                result = self.service.run([row], -33.8568, 151.2153, location_name='测试地点')
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['complete'], 1)
                after = photo_metadata(path)
                self.assertEqual(before['File:ImageDataHash'], after['File:ImageDataHash'])
                for tag in ('IFD0:Make', 'IFD0:Model', 'IFD0:Orientation', 'IFD0:ModifyDate'):
                    self.assertEqual(before.get(tag), after.get(tag))
                self.assertEqual(after['GPS:GPSLatitudeRef'], 'S')
                self.assertEqual(after['GPS:GPSLongitudeRef'], 'E')
                self.assertEqual(path.stat().st_mtime_ns, stamp)
                current = self.catalog.search(text=path.name)[1][0]
                self.assertEqual(current['gps_status'], 'present')
                self.assertEqual(current['content_hash'], digest(path))
                self.catalog.scan(self.source, retry_metadata=True)
                rescanned = self.catalog.search(text=path.name)[1][0]
                self.assertEqual(rescanned['gps_status'], 'present')
                self.assertAlmostEqual(rescanned['latitude'], -33.8568, places=5)
        history = ProcessingHistory(self.catalog).page()
        self.assertEqual(len(history), 4)
        self.assertEqual({r['record_type'] for r in history}, {'gps'})
        self.assertEqual(sum(r['completed_photos'] for r in history), 4)

    def test_existing_gps_skipped_until_explicit_overwrite(self):
        path, row = self.photo()
        self.assertEqual(self.service.run([row], 39.9, 116.4)['complete'], 1)
        current = self.catalog.search()[1][0]; fingerprint = digest(path)
        self.assertEqual(self.service.run([current], 20, 30)['skipped'], 1)
        self.assertEqual(digest(path), fingerprint)
        result = self.service.run([current], 20, 30, overwrite=True)
        self.assertEqual(result['errors'], [])
        self.assertEqual(photo_metadata(path)['GPS:GPSLatitude'], 20)
        with closing(self.catalog.connect()) as db:
            old = json.loads(db.execute('SELECT old_gps_json FROM gps_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(old['GPS:GPSLatitude'], 39.9)

    def test_bad_validation_keeps_original_and_removes_temp(self):
        path, row = self.photo(); fingerprint = digest(path)
        real = photo_metadata
        def corrupt(target, cancel=None):
            result = real(target, cancel)
            if Path(target).name.startswith('.mediaorganizer-'): result['File:ImageDataHash'] = 'broken'
            return result
        with patch('location_service.photo_metadata', side_effect=corrupt):
            result = self.service.run([row], 30, 120)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(digest(path), fingerprint)
        self.assertEqual(list(self.source.glob('.mediaorganizer-*')), [])
        self.assertEqual(ProcessingHistory(self.catalog).page()[0]['status'], 'failed')

    def test_ready_recovery_before_and_after_replace_and_blocks_import(self):
        for before_replace in (True, False):
            with self.subTest(before_replace=before_replace):
                path, row = self.photo(); fingerprint = digest(path)
                target = 'location_service.os.replace' if before_replace else None
                guard = patch(target, side_effect=OSError('simulated interruption')) if target else patch.object(self.service, '_finish', side_effect=OSError('simulated interruption'))
                with guard:
                    result = self.service.run([row], 30, 120)
                self.assertEqual(len(result['errors']), 1)
                if before_replace: self.assertEqual(digest(path), fingerprint)
                with self.assertRaises(ValueError):
                    ImportService(self.catalog).build_plan([row], self.source, self.base/'archive')
                result = self.service.run([row], 35, 125)
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['recovered'], 1)
                self.assertEqual(photo_metadata(path)['GPS:GPSLatitude'], 30)

    def test_external_modification_after_ready_is_not_replaced(self):
        path, row = self.photo()
        with patch('location_service.os.replace', side_effect=OSError('stop')):
            self.service.run([row], 30, 120)
        with path.open('ab') as file: file.write(b'external')
        fingerprint = digest(path)
        self.assertEqual(len(self.service.run([row], 30, 120)['errors']), 1)
        self.assertEqual(digest(path), fingerprint)

    def test_pending_import_and_stale_photo_rejected(self):
        path, row = self.photo(); fingerprint = digest(path)
        importer = ImportService(self.catalog)
        importer.save_plan(importer.build_plan([row], self.source, self.base/'archive'))
        self.assertEqual(len(self.service.run([row], 30, 120)['errors']), 1)
        self.assertEqual(digest(path), fingerprint)
        other, row2 = self.photo('.png'); other.write_bytes(other.read_bytes() + b'external')
        self.assertEqual(len(self.service.run([row2], 30, 120)['errors']), 1)

    def test_cancel_and_unsupported_do_not_write(self):
        path, row = self.photo(); fingerprint = digest(path)
        cancel = threading.Event(); cancel.set()
        self.assertTrue(self.service.run([row], 30, 120, cancel=cancel)['stopped'])
        self.assertEqual(self.service.run([dict(row, kind='video')], 30, 120)['skipped'], 1)
        self.assertEqual(digest(path), fingerprint)

    def test_coordinate_conversion_and_invalid_values(self):
        from geo_coordinates import wgs2gcj
        gps = (39.908823, 116.397470)
        gcj = wgs2gcj(*gps)
        restored = coordinates(*gcj, 'GCJ02')
        for actual, expected in zip(restored, gps): self.assertAlmostEqual(actual, expected, places=5)
        self.assertEqual(coordinates(-33.8,151.2,'GCJ02'), (-33.8,151.2))
        for values in ((float('nan'),1), (1,float('inf')), (91,1), (1,181)):
            with self.assertRaises(ValueError): coordinates(*values)

    def test_provider_results_and_error_redaction(self):
        body = {'status':'1','pois':[{'name':'测试地点','address':[], 'location':'116.4,39.9'}]}
        with patch('location_service.urlopen', return_value=io.BytesIO(json.dumps(body).encode())) as request:
            result = search_places('SECRET_KEY', '地点', '北京')
        self.assertEqual(result[0]['latitude'], 39.9)
        self.assertIn('https://restapi.amap.com/v3/place/text?', request.call_args.args[0])
        with patch('location_service.urlopen', side_effect=OSError('url?key=SECRET_KEY')):
            with self.assertRaises(ValueError) as exc: search_places('SECRET_KEY','地点')
        self.assertNotIn('SECRET_KEY', str(exc.exception))
        with patch('location_service.urlopen', return_value=io.BytesIO(b'{"status":"0","infocode":"10009"}')):
            with self.assertRaisesRegex(ValueError, 'Web 服务'): search_places('KEY','地点')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_key_encrypted_for_current_user(self):
        encrypted = protect_key('sample-api-key')
        self.assertNotIn('sample-api-key', encrypted)
        self.assertEqual(protect_key(encrypted, True), 'sample-api-key')

    def test_v3_migration_backup_and_existing_records_preserved(self):
        self.photo()
        with closing(self.catalog.connect()) as db, db:
            db.execute('PRAGMA user_version=3'); db.execute('DROP TABLE gps_jobs')
        self.catalog.initialize()
        self.assertEqual(self.catalog.search()[0], 1)
        self.assertEqual(len(list(self.catalog.database.parent.glob('*.pre-v7-*.backup'))), 1)


if __name__ == '__main__': unittest.main()
