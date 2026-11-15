import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from media_catalog import Catalog, PHOTO_EXTS
from import_service import ImportService, beneath, digest
from source_scope import roots, encode


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name).resolve()
        self.a = self.base/'相机 A'; self.b = self.base/'相机 B'; self.child = self.a/'子文件夹'
        self.child.mkdir(parents=True); self.b.mkdir()
        for i, folder in enumerate((self.a, self.b, self.child)):
            Image.new('RGB', (24, 16), (40+i*20,100,120)).save(folder/'2026-11-14_photo.jpg')
            Image.new('RGB', (24, 16), '#789945').save(folder/'photo.png')
        self.catalog = Catalog(self.base/'data/catalog.sqlite3'); self.catalog.initialize()

    def tearDown(self): self.temp.cleanup()

    def test_multi_shallow_and_recursive_overlap_deduplicate(self):
        for recursive, expected in ((False, 4), (True, 6)):
            result = self.catalog.scan([self.a, self.b, self.a], recursive=recursive)
            self.assertEqual(result['photos'], expected)
            self.assertEqual(self.catalog.search(batch_id=result['batch_id'])[0], expected)
        result = self.catalog.scan([self.child, self.a, self.b], recursive=True)
        self.assertEqual(result['photos'], 6)
        result = self.catalog.scan([self.child, self.a], recursive=False)
        self.assertEqual(result['photos'], 4)

    def test_extension_filter_precedes_metadata_and_preserves_unselected_availability(self):
        self.catalog.scan(self.a, recursive=True)
        video = self.a/'excluded.mp4'; video.write_bytes(b'not a video')
        from media_catalog import read_metadata
        with patch('media_catalog.read_metadata', wraps=read_metadata) as read:
            result = self.catalog.scan([self.a, self.b], recursive=False, extensions={'.jpg'}, retry_metadata=True)
        self.assertEqual(result['photos'], 2); self.assertEqual(result['videos'], 0)
        self.assertEqual({Path(c.args[0]).suffix for c in read.call_args_list}, {'.jpg'})
        self.assertTrue(all(r['availability']=='present' for r in self.catalog.search()[1]))
        (self.a/'2026-11-14_photo.jpg').unlink()
        self.catalog.scan(self.a, recursive=False, extensions={'.jpg'})
        rows = self.catalog.search()[1]
        self.assertEqual(next(r for r in rows if r['current_path']==str(self.a/'2026-11-14_photo.jpg'))['availability'], 'not_seen')
        self.assertTrue(all(r['availability']=='present' for r in rows if r['current_path']!=str(self.a/'2026-11-14_photo.jpg')))

    def test_invalid_scope_and_empty_format_do_not_start_partial_scan(self):
        for selected, extensions in (([self.a, self.base/'absent'], None), ([self.a, self.base], None), ([self.a], set())):
            with self.assertRaises((ValueError, OSError)):
                self.catalog.scan(selected, extensions=extensions)
            self.assertEqual(self.catalog.batches(), [])

    def test_multi_import_serializes_scope_and_recovery_bounds(self):
        result = self.catalog.scan([self.a, self.b], recursive=False, extensions={'.jpg'})
        rows = self.catalog.search(batch_id=result['batch_id'])[1]
        before = {r['current_path']:digest(r['current_path']) for r in rows}
        service = ImportService(self.catalog)
        plan = service.build_plan(rows, [self.a, self.b], self.base/'archive')
        self.assertEqual(roots(plan['source_root']), [self.a.resolve(), self.b.resolve()])
        identifier = service.save_plan(plan, 'copy')
        self.assertEqual(ImportService(self.catalog).run(identifier)['complete'], 2)
        self.assertEqual(self.catalog.search(imported_only=True)[0], 2)
        self.assertEqual({p:digest(p) for p in before}, before)
        self.assertEqual(len(set(r['target_path'] for r in service.items(identifier))), 2)
        with self.assertRaises(ValueError): beneath(self.base/'other/file.jpg', encode([self.a,self.b]))
        with self.assertRaises(ValueError): service.build_plan(rows, [self.a,self.b], self.b/'output')

    def test_cancel_does_not_mark_unvisited_media_missing(self):
        self.catalog.scan([self.a, self.b], recursive=True)
        cancel = threading.Event(); cancel.set()
        result = self.catalog.scan([self.a, self.b], recursive=True, cancel=cancel)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(all(r['availability']=='present' for r in self.catalog.search()[1]))
