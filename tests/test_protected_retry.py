import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from imageio_ffmpeg import get_ffmpeg_exe
from media_catalog import Catalog
from task_queue import TaskQueue, can_use_protected
from import_service import digest
from processing_history import ProcessingHistory


class ProtectedRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        source=self.base/'source';source.mkdir()
        first=source/'DJI_001.mp4'
        subprocess.run([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi','-i',
            'testsrc2=size=64x64:rate=5','-t','0.4','-c:v','libx264','-movflags','+faststart',str(first)],
            capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        (source/'DJI_002.mp4').write_bytes(first.read_bytes())
        self.catalog=Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize();self.catalog.scan(source)
        self.rows=self.catalog.search()[1];self.hashes={r['current_path']:digest(r['current_path']) for r in self.rows}
        self.queue=TaskQueue(self.catalog)
        self.options=dict(latitude=30.,longitude=120.,write_mode='fast',overwrite=True)
        self.queue.submit('gps',self.rows,self.options);self.drain()

    def drain(self):
        deadline=time.monotonic()+20
        while self.queue.active and time.monotonic()<deadline:self.queue.tick();time.sleep(.01)
        self.assertFalse(self.queue.active)

    def tearDown(self):
        self.queue.control(None,'stop');self.drain();self.queue.shutdown();self.temp.cleanup()

    def test_only_confirmed_file_retries_and_original_skip_remains(self):
        before=self.queue.snapshot();self.assertTrue(all(can_use_protected(r) for r in before))
        self.assertEqual({p:digest(p) for p in self.hashes},self.hashes)
        selected=before[0]['id']
        with self.assertRaises(ValueError):self.queue.submit_protected([selected])
        self.assertEqual(len(self.queue.snapshot()),2)
        self.queue.submit_protected([selected],confirmed=True);self.drain()
        after=self.queue.snapshot();self.assertEqual(after[-1]['status'],'complete',after)
        self.assertEqual(after[-1]['retry_of'],selected)
        self.assertEqual(json.loads(after[-1]['options_json'])['write_mode'],'protected')
        self.assertEqual(after[0]['status'],'skipped')
        other=before[1]['source_path'];self.assertEqual(digest(other),self.hashes[other])
        records=ProcessingHistory(self.catalog).page(record_type='gps');self.assertEqual(len(records),3)
        self.assertEqual([r['id'] for r in self.queue.protected_candidates(selected)],[before[1]['id']])
        with self.assertRaises(ValueError):self.queue.submit_protected([selected],confirmed=True)

    def test_batch_excludes_other_batches_and_preserves_parameters(self):
        original=self.queue.snapshot()
        identifiers=[r['id'] for r in original]
        self.queue.submit_protected(identifiers,confirmed=True);self.drain()
        latest=self.queue.snapshot()[2:]
        self.assertTrue(all(r['status']=='complete' for r in latest),latest)
        self.assertEqual({r['retry_of'] for r in latest},set(identifiers))
        for r in latest:
            options=json.loads(r['options_json']);self.assertTrue(options['overwrite'])
            self.assertEqual(options['latitude'],30.);self.assertEqual(options['longitude'],120.)
        self.assertEqual(self.queue.protected_candidates(identifiers[0]),[])

    def test_change_or_active_retry_is_rejected(self):
        selected=self.queue.snapshot()[0]
        self.queue.submit_protected([selected['id']],confirmed=True)
        with self.assertRaises(ValueError):self.queue.submit_protected([selected['id']],confirmed=True)
        self.drain()
        second=self.queue.snapshot()[1]
        Path(second['source_path']).write_bytes(b'external change')
        self.queue.submit_protected([second['id']],confirmed=True);self.drain()
        self.assertEqual(self.queue.snapshot()[-1]['status'],'failed')
        self.assertEqual(Path(second['source_path']).read_bytes(),b'external change')

    def test_cannot_combine_different_confirmed_batches(self):
        first=self.queue.snapshot()[0]['id']
        self.queue.submit('gps',[self.rows[0]],dict(self.options,latitude=31.));self.drain()
        other=self.queue.snapshot()[-1]['id']
        with self.assertRaises(ValueError):self.queue.submit_protected([first,other],confirmed=True)
        self.assertEqual(len(self.queue.snapshot()),3)


if __name__=='__main__':unittest.main()
