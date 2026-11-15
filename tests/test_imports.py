import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from media_catalog import Catalog
from import_service import ImportService, age_text, digest
from thumbnail_cache import ThumbnailCache


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.target = self.base / 'target'
        self.source.mkdir()
        self.catalog = Catalog(self.base / 'local' / 'catalog.sqlite3')
        self.catalog.initialize()
        self.cache = ThumbnailCache(self.base / 'local' / 'thumbs')
        self.service = ImportService(self.catalog, self.cache)

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, name='DSC_0001.jpg', color='green', dated=True):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        exif = Image.Exif()
        exif[272] = 'Test Camera'
        if dated:
            exif[34665] = {36867: '2025:03:01 10:11:12'}
        Image.new('RGB', (1800, 1200), color).save(path, exif=exif)
        return path

    def plan(self, mode='move', **options):
        scan = self.catalog.scan(self.source, batch_kind='source_scan')
        rows = self.catalog.search(batch_id=scan['batch_id'], limit=10000)[1]
        plan = self.service.build_plan(rows, self.source, self.target, options)
        identifier = self.service.save_plan(plan, mode)
        return identifier, plan

    def test_preview_has_no_file_effects_and_move_retains_original_query(self):
        source = self.photo()
        original_hash = digest(source)
        identifier, plan = self.plan(place='天津', birthday='2025-01-31')
        self.assertFalse(self.target.exists())
        self.assertTrue(source.exists())
        result = self.service.run(identifier)
        self.assertEqual(result['complete'], 1)
        self.assertFalse(source.exists())
        final = Path(plan['items'][0]['target_path'])
        self.assertTrue(final.exists())
        self.assertEqual(digest(final), original_hash)
        self.assertIn('1个月1天', str(final))
        row = self.catalog.search('DSC_0001')[1][0]
        self.assertEqual(row['original_name'], 'DSC_0001.jpg')
        self.assertEqual(row['place'], '天津')
        self.assertEqual(row['current_path'], str(final))
        self.assertTrue(self.cache.lookup(row))
        with patch('thumbnail_cache.Image.open', side_effect=AssertionError('must use cache')):
            self.assertTrue(self.cache.ensure(row))
        history = self.service.history()[0]
        self.assertEqual((history['photos'], history['videos'], history['complete']), (1, 0, 1))
        self.assertEqual(self.service.run(identifier)['complete'], 1)

    def test_copy_keeps_original_and_dedup_keeps_second_source(self):
        source = self.photo()
        identifier, plan = self.plan(mode='copy', template='{date}_{device}')
        self.service.run(identifier)
        self.assertTrue(source.exists())
        self.assertEqual(self.catalog.search('DSC_0001')[0], 1)
        second, _ = self.plan(mode='move', template='{date}_{device}')
        result = self.service.run(second)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(source.exists())
        self.assertEqual(len(list(self.target.rglob('*.jpg'))), 1)

    def test_two_identical_names_in_different_directories_not_overwritten(self):
        self.photo('a/DSC_0001.jpg', 'green')
        self.photo('b/DSC_0001.jpg', 'red')
        identifier, plan = self.plan()
        result = self.service.run(identifier)
        self.assertEqual(result['complete'], 2)
        self.assertNotEqual(plan['items'][0]['target_path'], plan['items'][1]['target_path'])
        self.assertEqual(self.catalog.search('DSC_0001')[0], 2)

    def test_same_content_multiple_aliases_in_one_import(self):
        first = self.photo('ONE.jpg')
        (self.source / 'TWO.jpg').write_bytes(first.read_bytes())
        (self.source / 'THREE.jpg').write_bytes(first.read_bytes())
        identifier, _ = self.plan(template='{date}')
        result = self.service.run(identifier)
        self.assertEqual((result['complete'], result['skipped']), (1, 2))
        self.assertEqual(len(list(self.target.rglob('*.jpg'))), 1)
        for name in ('ONE.jpg', 'TWO.jpg', 'THREE.jpg'):
            rows = self.catalog.search(name)[1]
            self.assertTrue(any(Path(r['current_path']).is_relative_to(self.target.resolve()) for r in rows))

    def test_collision_appearing_after_preview_is_never_overwritten(self):
        self.photo()
        identifier, plan = self.plan()
        desired = Path(plan['items'][0]['target_path'])
        desired.parent.mkdir(parents=True)
        desired.write_bytes(b'existing unrelated user file')
        result = self.service.run(identifier)
        self.assertEqual(result['complete'], 1)
        self.assertEqual(desired.read_bytes(), b'existing unrelated user file')
        self.assertNotEqual(self.service.items(identifier)[0]['target_path'], str(desired))

    def test_mismatched_copy_retains_source_and_retry_recovers(self):
        source = self.photo()
        identifier, _ = self.plan()
        real = digest
        def corrupt_verification(path, cancel=None):
            return 'mismatch' if Path(path).suffix == '.part' else real(path, cancel)
        with patch('import_service.digest', corrupt_verification):
            result = self.service.run(identifier)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(source.exists())
        result = self.service.run(identifier)
        self.assertEqual(result['complete'], 1)
        self.assertFalse(source.exists())
        self.assertEqual(len(list(self.target.rglob('*.part'))), 0)

    def test_recover_after_publish_before_source_delete(self):
        source = self.photo()
        identifier, _ = self.plan()
        with patch.object(self.service, '_finish_move', side_effect=OSError('simulated disconnect')):
            result = self.service.run(identifier)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(source.exists())
        self.assertEqual(len(list(self.target.rglob('*.jpg'))), 1)
        self.assertEqual(self.service.run(identifier)['complete'], 1)
        self.assertFalse(source.exists())
        self.assertEqual(len(list(self.target.rglob('*.jpg'))), 1)

    def test_recover_after_source_delete_before_completion_record(self):
        source = self.photo()
        identifier, _ = self.plan()
        real_update = self.service._update
        def interrupted(item_id, **values):
            if values.get('status') == 'complete':
                raise OSError('simulated database completion failure')
            return real_update(item_id, **values)
        with patch.object(self.service, '_update', interrupted):
            self.service.run(identifier)
        self.assertFalse(source.exists())
        self.assertEqual(self.service.run(identifier)['complete'], 1)

    def test_source_change_after_preview_does_not_delete_or_copy(self):
        source = self.photo()
        identifier, _ = self.plan()
        source.write_bytes(b'user updated source')
        self.assertEqual(self.service.run(identifier)['errors'], 1)
        self.assertEqual(source.read_bytes(), b'user updated source')
        self.assertFalse(self.target.exists())

    def test_stop_and_resume_only_pending_items(self):
        self.photo('one.jpg', 'green')
        self.photo('two.jpg', 'red')
        identifier, _ = self.plan()
        stop = threading.Event()
        result = self.service.run(identifier, stop, progress=lambda p: stop.set() if p['phase'] == 'finished' else None)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['complete'], 1)
        self.assertEqual(self.service.run(identifier)['complete'], 2)
        self.assertEqual(len(list(self.target.rglob('*.jpg'))), 2)

    def test_unknown_date_and_path_escape_input(self):
        self.photo(dated=False)
        identifier, plan = self.plan(place='../outside')
        final = Path(plan['items'][0]['target_path'])
        self.assertTrue(final.is_relative_to(self.target.resolve()))
        self.assertIn('待确认日期', str(final))
        self.assertEqual(self.service.run(identifier)['complete'], 1)

    def test_overlapping_directories_rejected(self):
        self.photo()
        self.catalog.scan(self.source)
        with self.assertRaises(ValueError):
            self.service.build_plan(self.catalog.search()[1], self.source, self.source / 'nested')

    def test_migration_keeps_v1_records_and_creates_backup(self):
        self.photo()
        self.catalog.scan(self.source)
        with closing(self.catalog.connect()) as db, db:
            db.execute('DROP INDEX idx_media_hash')
            for field in ('rating', 'place', 'content_hash', 'imported_at'):
                db.execute('ALTER TABLE media DROP COLUMN ' + field)
            for table in ('name_history', 'import_items', 'imports'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=1')
        self.catalog.initialize()
        self.assertEqual(self.catalog.search()[0], 1)
        backups = list(self.catalog.database.parent.glob('*.backup'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM media').fetchone()[0], 1)

    def test_thumbnail_is_small_source_untouched_and_cache_bounded(self):
        source = self.photo()
        before = digest(source), source.stat().st_mtime_ns
        self.catalog.scan(self.source)
        row = self.catalog.search()[1][0]
        preview = self.cache.ensure(row)
        with Image.open(preview) as image:
            self.assertLessEqual(max(image.size), 480)
        self.assertEqual(before, (digest(source), source.stat().st_mtime_ns))
        self.cache.budget = 1
        self.cache.prune()
        self.assertFalse(preview.exists())

    def test_age_month_end(self):
        self.assertEqual(age_text('2025-01-31', '2025-03-01'), '1个月1天')
        self.assertEqual(age_text('2024-02-29', '2025-02-28'), '1岁')

    def test_old_import_batches_remain_accessible(self):
        with closing(self.catalog.connect()) as db, db:
            db.executemany('INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)',
                [(f'batch-{i}', '2025-01-01', None, str(self.source), str(self.target),
                  'copy', '{}', 'pending') for i in range(205)])
        first = self.service.history()
        older = self.service.history(offset=200)
        self.assertEqual((len(first), len(older)), (200, 5))
        self.assertEqual(len({r['id'] for r in first + older}), 205)


if __name__ == '__main__':
    unittest.main()
