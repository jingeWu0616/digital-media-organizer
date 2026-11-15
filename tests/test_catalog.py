import hashlib
import io
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from media_catalog import Catalog, guessed_original, read_metadata, require_local_database


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'media'
        self.source.mkdir()
        self.catalog = Catalog(self.base / 'local' / 'catalog.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, name='DSC_0001.jpg', gps=False):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        exif = Image.Exif()
        exif[272] = 'Test Camera'
        exif[34665] = {36867: '2024:03:02 10:11:12'}
        if gps:
            exif[34853] = {1: 'N', 2: (39.0, 30.0, 0.0), 3: 'E', 4: (116.0, 15.0, 0.0)}
        Image.new('RGB', (30, 20), 'green').save(path, exif=exif)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.source)): (p.stat().st_size, p.stat().st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.source.rglob('*') if p.is_file()}

    def test_inventory_is_readonly_and_not_import_history(self):
        self.photo()
        self.photo('sub/2024-03-02_Sat_Camera_Tianjin_DSC_0002.jpg', gps=True)
        (self.source / 'notes.txt').write_text('do not touch', encoding='utf-8')
        before = self.snapshot()
        stats = self.catalog.scan(self.source)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(stats['photos'], 2)
        self.assertEqual(stats['ignored'], 1)
        self.assertEqual(stats['status'], 'completed')
        total, rows = self.catalog.search()
        self.assertEqual(total, 2)
        self.assertTrue(all(row['original_name'] is None for row in rows))
        self.assertTrue(all(row['date_source'] == 'metadata' for row in rows))
        batches = self.catalog.batches()
        self.assertEqual(batches[0]['kind'], 'historical_index')
        self.assertNotEqual(rows[0]['capture_time'][:10], batches[0]['started_at'][:10])
        self.assertEqual(self.catalog.search('DSC_0002')[0], 1)
        self.assertEqual(self.catalog.search(gps='present')[1][0]['latitude'], 39.5)
        self.assertEqual(self.catalog.search(gps='missing')[0], 1)

    def test_shallow_scan_does_not_read_children_or_mark_them_missing(self):
        direct = self.photo('direct.jpg')
        child = self.photo('sub/child.jpg')
        self.catalog.scan(self.source)
        direct.unlink()
        new = self.photo('new.jpg')
        with patch('media_catalog.read_metadata', wraps=read_metadata) as read:
            stats = self.catalog.scan(self.source, recursive=False)
            read.assert_called_once_with(new, 'photo')
        self.assertEqual(stats['photos'], 1)
        self.assertEqual([r['observed_name'] for r in self.catalog.search(batch_id=stats['batch_id'])[1]], ['new.jpg'])
        rows = {r['observed_name']: r for r in self.catalog.search()[1]}
        self.assertEqual(rows['direct.jpg']['availability'], 'not_seen')
        self.assertNotEqual(rows['child.jpg']['availability'], 'not_seen')
        self.assertTrue(child.exists())

    def test_rescan_reuses_metadata_but_has_separate_batch(self):
        path = self.photo()
        self.catalog.scan(self.source)
        with patch('media_catalog.read_metadata', side_effect=AssertionError('must use cache')):
            stats = self.catalog.scan(self.source)
        self.assertEqual(stats['unchanged'], 1)
        self.assertEqual(self.catalog.search()[0], 1)
        self.assertEqual(len(self.catalog.batches()), 2)
        with patch('media_catalog.read_metadata', wraps=read_metadata) as read:
            self.catalog.scan(self.source, retry_metadata=True)
            read.assert_called_once_with(path, 'photo')

    def test_corrupt_file_does_not_stop_batch_or_claim_missing_gps(self):
        self.photo()
        broken = self.source / '2020-02-29_Sat_cam_IMG_0042.jpg'
        broken.write_bytes(b'not an image')
        (self.source / 'broken.mp4').write_bytes(b'not a video')
        stats = self.catalog.scan(self.source)
        self.assertEqual(stats['status'], 'completed_with_errors')
        self.assertEqual(stats['photos'], 2)
        self.assertEqual(stats['videos'], 1)
        self.assertEqual(self.catalog.search()[0], 3)
        row = self.catalog.search('IMG_0042')[1][0]
        self.assertEqual(row['date_source'], 'filename_inferred')
        self.assertEqual(row['gps_status'], 'unknown')
        self.assertEqual(row['inferred_original'], 'IMG_0042.jpg')
        self.assertIsNone(row['original_name'])
        self.assertGreaterEqual(len(self.catalog.errors(stats['batch_id'])), 2)

    def test_real_video_duration_without_assuming_missing_gps(self):
        # Generate a two-frame MJPEG AVI fixture entirely in the temp folder.
        image = io.BytesIO()
        Image.new('RGB', (30, 20), 'blue').save(image, format='JPEG')
        jpeg = image.getvalue()
        def chunk(tag, payload):
            return tag + struct.pack('<I', len(payload)) + payload + b'\0' * (len(payload) % 2)
        avih = struct.pack('<14I', 1000000, len(jpeg), 0, 0, 2, 0, 1, len(jpeg), 30, 20, 0, 0, 0, 0)
        strh = struct.pack('<4s4sIHHIIIIIIIIhhhh', b'vids', b'MJPG', 0, 0, 0, 0, 1, 1, 0, 2, len(jpeg), 0xffffffff, 0, 0, 0, 30, 20)
        strf = struct.pack('<IiiHH4sIiiII', 40, 30, 20, 1, 24, b'MJPG', len(jpeg), 0, 0, 0, 0)
        stream = chunk(b'LIST', b'strl' + chunk(b'strh', strh) + chunk(b'strf', strf))
        header = chunk(b'LIST', b'hdrl' + chunk(b'avih', avih) + stream)
        movie = chunk(b'LIST', b'movi' + chunk(b'00dc', jpeg) * 2)
        path = self.source / 'clip.avi'
        path.write_bytes(chunk(b'RIFF', b'AVI ' + header + movie))
        result = self.catalog.scan(self.source)
        self.assertEqual(result['videos'], 1)
        self.assertEqual(result['errors'], 0)
        row = self.catalog.search()[1][0]
        self.assertAlmostEqual(row['duration_seconds'], 2.0)
        self.assertEqual(row['gps_status'], 'unknown')
        self.assertIsNone(row['capture_time'])

    def test_cancellation_keeps_previous_inventory_and_can_resume(self):
        self.photo()
        first = self.catalog.scan(self.source)
        stopped = threading.Event()
        stopped.set()
        stats = self.catalog.scan(self.source, stopped)
        self.assertEqual(stats['status'], 'cancelled')
        self.assertEqual(self.catalog.search()[1][0]['availability'], 'present')
        self.assertEqual(self.catalog.search(batch_id=first['batch_id'])[0], 1)
        self.assertEqual(self.catalog.scan(self.source)['status'], 'completed')

    def test_missing_files_flagged_only_after_complete_scan(self):
        path = self.photo()
        self.catalog.scan(self.source)
        path.unlink()  # test-owned fixture only
        def failing_walk(root, onerror, followlinks):
            onerror(PermissionError('test denied'))
            return iter(())
        with patch('media_catalog.os.walk', failing_walk):
            self.catalog.scan(self.source)
        self.assertEqual(self.catalog.search()[1][0]['availability'], 'present')
        self.catalog.scan(self.source)
        self.assertEqual(self.catalog.search()[1][0]['availability'], 'not_seen')

    def test_names_are_not_used_as_unique_identity(self):
        self.photo('camera_a/DSC_0001.jpg')
        self.photo('camera_b/DSC_0001.jpg')
        self.catalog.scan(self.source)
        total, rows = self.catalog.search('DSC_0001.jpg')
        self.assertEqual(total, 2)
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])

    def test_search_handles_literal_wildcards_and_quotes(self):
        self.photo('100%_旅行.jpg')
        self.photo('100x_旅行.jpg')
        self.catalog.scan(self.source)
        self.assertEqual(self.catalog.search('100%')[0], 1)
        self.assertEqual(self.catalog.search("' OR 1=1 --")[0], 0)
        self.assertEqual(self.catalog.search('旅行')[0], 2)

    def test_database_cannot_be_written_into_source_or_unc(self):
        bad = Catalog(self.source / 'db.sqlite3')
        with self.assertRaises(ValueError):
            bad.scan(self.source)
        self.assertFalse((self.source / 'db.sqlite3').exists())
        if os.name == 'nt':
            with self.assertRaises(ValueError):
                require_local_database(r'\\server\share\catalog.sqlite3')

    def test_changed_source_is_not_recorded_as_success(self):
        path = self.photo()
        def changing_read(file, kind):
            result = read_metadata(file, kind)
            with file.open('ab') as stream:
                stream.write(b'test concurrent write')
            return result
        with patch('media_catalog.read_metadata', changing_read):
            result = self.catalog.scan(self.source)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(self.catalog.search()[0], 0)

    def test_original_name_inference_is_explicit_and_conservative(self):
        self.assertIsNone(guessed_original('DSC_0001.jpg'))
        self.assertIsNone(guessed_original('2024-01-01_Mon_设备_地点_旅行照片.jpg'))
        self.assertEqual(guessed_original('2024-01-01_Mon_Cam_City_IMG_0012.JPG'), 'IMG_0012.JPG')


if __name__ == '__main__':
    unittest.main()
