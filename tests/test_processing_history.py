import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from PIL import Image
from media_catalog import Catalog
from import_service import ImportService
from processing_history import ProcessingHistory


class ProcessingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        Image.new('RGB', (20, 20), 'green').save(self.source / 'IMG_001.jpg')
        self.catalog = Catalog(self.base / 'local/catalog.sqlite3')
        self.catalog.initialize()
        self.catalog.scan(self.source)
        self.media = self.catalog.search()[1][0]
        self.history = ProcessingHistory(self.catalog)

    def tearDown(self):
        self.temp.cleanup()

    def add_color(self, identifier, created_at, status='complete'):
        with closing(self.catalog.connect()) as db, db:
            db.execute('''INSERT INTO color_jobs(id,media_id,source_path,source_name,created_at,
                status,lut_hash,temp_path,encoder,error) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (identifier, self.media['id'], self.media['current_path'], 'DJI_0001.mp4',
                 created_at, status, 'test-lut', 'test.part', 'hevc_qsv', 'test error' if status == 'failed' else None))

    def test_merge_existing_records_stats_details_and_type_filters(self):
        service = ImportService(self.catalog)
        plan = service.build_plan([self.media], self.source, self.base / 'archive')
        identifier = service.save_plan(plan, 'copy')
        service.run(identifier)
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE imports SET created_at=? WHERE id=?', ('2026-11-14T10:00:00+08:00', identifier))
        self.add_color(identifier, '2026-11-14T03:00:00+00:00', 'failed')
        rows = self.history.page()
        self.assertEqual([r['record_type'] for r in rows], ['color', 'import'])
        self.assertEqual(len({r['record_key'] for r in rows}), 2)
        self.assertEqual(rows[0]['completed_videos'], 0)
        self.assertEqual(rows[0]['errors'], 1)
        self.assertEqual(rows[1]['completed_photos'], 1)
        self.assertEqual(rows[1]['type_label'], '导入 · 复制')
        self.assertEqual(self.history.page(record_type='import')[0]['id'], identifier)
        self.assertEqual(len(self.history.page(record_type='color')), 1)
        color = self.history.details('color', identifier)[0]
        self.assertEqual(color['encoder'], 'hevc_qsv')
        self.assertEqual(color['error'], 'test error')
        self.assertEqual(color['current_path'], self.history.media(self.media['id'])['current_path'])
        self.assertEqual(self.history.details('import', identifier)[0]['source_name'], 'IMG_001.jpg')

    def test_global_pagination_does_not_drop_records_from_either_source(self):
        timestamp = '2026-11-14T10:00:00+08:00'
        with closing(self.catalog.connect()) as db, db:
            db.executemany('''INSERT INTO imports(id,created_at,source_root,target_root,mode,options_json,status)
                VALUES(?,?,?,?,'move','{}','completed')''',
                [(str(i), timestamp, str(self.source), str(self.base / 'archive')) for i in range(215)])
            db.executemany('''INSERT INTO color_jobs(id,media_id,source_path,source_name,created_at,status,lut_hash,temp_path)
                VALUES(?,?,?,?,?,'complete','test','test.part')''',
                [(str(i), self.media['id'], self.media['current_path'], f'DJI_{i}.mp4', timestamp) for i in range(215)])
        pages = [self.history.page(limit=200, offset=offset) for offset in (0, 200, 400)]
        self.assertEqual([len(p) for p in pages], [200, 200, 30])
        keys = [r['record_key'] for p in pages for r in p]
        self.assertEqual(len(set(keys)), 430)
        self.assertEqual(keys, [r['record_key'] for r in self.history.page(limit=500)])
        self.assertEqual(len(self.history.page(record_type='color', offset=200)), 15)


if __name__ == '__main__':
    unittest.main()
