import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image
from fast_video_gps import box, plan, commit, locked_file
from import_service import digest, ImportService
from location_service import LocationService
from media_catalog import Catalog, read_metadata
from processing_history import ProcessingHistory
from video_gps import integrity


class FastGpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.tail = Path(cls.fixtures.name)/'tail.mp4'
        subprocess.run([get_ffmpeg_exe(), '-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25',
                        '-f','lavfi','-i','sine=frequency=440','-t','0.4','-c:v','libx264','-c:a','aac',
                        str(cls.tail)], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        # Unknown private metadata must be byte-preserved, not reserialized.
        with cls.tail.open('rb') as file: offset, before, _ = plan(file, 0, 0)
        cls.tail.write_bytes(cls.tail.read_bytes()[:offset] + box(b'moov', before[8:]+box(b'uuid', b'private gyro bytes'*32)))
        cls.front = Path(cls.fixtures.name)/'front.mp4'
        subprocess.run([get_ffmpeg_exe(),'-v','error','-i',str(cls.tail),'-c','copy','-movflags','+faststart',str(cls.front)],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)

    @classmethod
    def tearDownClass(cls): cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base/'source'; self.source.mkdir()
        self.catalog = Catalog(self.base/'data/catalog.sqlite3'); self.catalog.initialize()
        self.service = LocationService(self.catalog)

    def tearDown(self): self.temp.cleanup()

    def video(self, name='clip.mp4', front=False):
        path = self.source/name; shutil.copy2(self.front if front else self.tail, path)
        self.catalog.scan(self.source)
        return path, self.catalog.search(text=name)[1][0]

    def test_tail_preserves_all_payload_and_private_metadata_without_full_copy(self):
        path, row = self.video(); before = integrity(path, threading.Event()); events=[]
        with patch('video_gps.write_verified', side_effect=AssertionError('no temporary video')):
            result = self.service.run([row], -33.8568, 151.2153, write_mode='fast', progress=events.append)
        self.assertEqual(result['complete'], 1, result)
        self.assertEqual(before, integrity(path, threading.Event()))
        self.assertAlmostEqual(read_metadata(path, 'video')['latitude'], -33.8568)
        current = self.catalog.search()[1][0]
        self.assertEqual(current['content_hash'], digest(path))
        self.assertEqual(current['mtime_ns'],path.stat().st_mtime_ns)
        self.assertEqual(row['mtime_ns'],path.stat().st_mtime_ns)
        self.assertEqual([p.name for p in self.source.iterdir()], [path.name])
        self.assertFalse(list((self.base/'data/gps_recovery').glob('*')))
        self.assertEqual(events[-1]['processed'], 1)
        self.assertIn('快速写入定位', [e['phase'] for e in events])
        self.assertEqual(ProcessingHistory(self.catalog).page()[0]['type_label'], '写入定位 · 快速')

    def test_explicit_overwrite_round_trip_and_existing_location_skip(self):
        path, row = self.video()
        self.assertEqual(self.service.run([row], 90, -180, write_mode='fast')['complete'], 1)
        before = digest(path); row = self.catalog.search()[1][0]
        self.assertEqual(self.service.run([row], 0, 0, write_mode='fast')['skipped'], 1)
        self.assertEqual(digest(path), before)
        for latitude, longitude in ((0.0000001,-0.0000001),(-90,180),(8,3)):
            row = self.catalog.search()[1][0]
            result = self.service.run([row], latitude, longitude, overwrite=True, write_mode='fast')
            self.assertEqual(result['complete'], 1, result)
            self.assertAlmostEqual(read_metadata(path,'video')['latitude'], latitude, places=7)
            self.assertAlmostEqual(read_metadata(path,'video')['longitude'], longitude, places=7)

    def test_front_and_fragmented_skip_without_writing_or_falling_back(self):
        for name, front in (('front.mp4',True),('fragmented.mp4',False)):
            path, row = self.video(name, front)
            if not front:
                with path.open('ab') as file: file.write(box(b'moof',b''))
                self.catalog.scan(self.source); row = self.catalog.search(text=name)[1][0]
            before = digest(path)
            with patch('video_gps.write_verified', side_effect=AssertionError('no automatic fallback')):
                result = self.service.run([row], 30, 120, write_mode='fast')
            self.assertEqual(result['skipped'], 1, result); self.assertEqual(result['errors'], [])
            self.assertEqual(len(result['skip_reasons']), 1)
            self.assertEqual(digest(path), before)

    def test_interruption_before_during_after_patch_recovers_previous_coordinates(self):
        for position in ('before','partial','after','database'):
            with self.subTest(position=position):
                path, row = self.video(position+'.mp4')
                signature = integrity(path,threading.Event())
                def interrupted(stream, offset, after):
                    if position == 'partial':
                        stream.seek(offset); stream.write(after[:len(after)-10]); os.fsync(stream.fileno())
                    elif position == 'after': commit(stream,offset,after)
                    raise OSError('simulated disconnect')
                guard = patch.object(self.service,'_finish',side_effect=OSError('db unavailable')) if position=='database' else patch('fast_video_gps.commit',side_effect=interrupted)
                with guard: result = self.service.run([row],30,120,write_mode='fast')
                self.assertEqual(len(result['errors']),1,result)
                with self.assertRaises(ValueError): ImportService(self.catalog).build_plan([row],self.source,self.base/'archive')
                result = self.service.run([row],20,110,write_mode='protected')
                self.assertEqual(result['recovered'],1,result)
                self.assertEqual(integrity(path,threading.Event()),signature)
                self.assertAlmostEqual(read_metadata(path,'video')['latitude'],30)

    def test_recovery_rejects_external_payload_change(self):
        path,row=self.video()
        with patch('fast_video_gps.commit',side_effect=OSError('disconnect')):
            self.service.run([row],30,120,write_mode='fast')
        data=bytearray(path.read_bytes()); data[data.index(b'mdat')+10] ^= 1; path.write_bytes(data)
        changed=digest(path)
        result=self.service.run([row],30,120,write_mode='fast')
        self.assertEqual(len(result['errors']),1,result)
        self.assertEqual(digest(path),changed)

    def test_cancel_and_exclusive_writer_lock(self):
        path,row=self.video(); before=digest(path)
        with locked_file(path):
            with self.assertRaises(OSError): path.open('r+b')
        cancel=threading.Event()
        def progress(event):
            if event['phase']=='核对原片': cancel.set()
        result=self.service.run([row],30,120,write_mode='fast',cancel=cancel,progress=progress)
        self.assertTrue(result['stopped']); self.assertEqual(digest(path),before)
        self.assertEqual(ProcessingHistory(self.catalog).page(),[])

    def test_mixed_photos_still_protected_and_color_marker_preserved(self):
        path,row=self.video()
        photo=self.source/'photo.jpg'; Image.new('RGB',(24,16),'red').save(photo)
        self.catalog.scan(self.source)
        with closing(self.catalog.connect()) as db, db:
            db.execute("UPDATE media SET color_revision='already-restored' WHERE id=?",(row['id'],))
        result=self.service.run(self.catalog.search()[1],30,120,write_mode='fast')
        self.assertEqual(result['complete'],2,result)
        current=self.catalog.search(text=path.name)[1][0]
        self.assertEqual(current['color_revision'],'already-restored')
        self.assertEqual(current['content_hash'],digest(path))
        self.catalog.scan(self.source)
        self.assertEqual(self.catalog.search(text=path.name)[1][0]['color_revision'],'already-restored')
        from color_service import ColorService
        self.assertEqual(ColorService(self.catalog).run([current],self.source,confirmed_dlogm=True)['skipped'],1)

    def test_smb_loopback(self):
        path,row=self.video()
        source=Path('\\\\localhost\\'+path.drive[0]+'$'+str(self.source)[2:])
        self.assertTrue(source.is_dir(), 'loopback share unavailable')
        remote=source/path.name
        catalog=Catalog(self.base/'smb/catalog.sqlite3'); catalog.initialize(); catalog.scan(source)
        row=catalog.search()[1][0]; signature=integrity(remote,threading.Event())
        result=LocationService(catalog).run([row],30,120,write_mode='fast')
        self.assertEqual(result['complete'],1,result)
        self.assertEqual(integrity(remote,threading.Event()),signature)

    def test_metadata_recovery_survives_catalog_backup_and_new_data_directory(self):
        path,row=self.video()
        with patch('fast_video_gps.commit',side_effect=OSError('disconnect')):
            self.assertEqual(len(self.service.run([row],30,120,write_mode='fast')['errors']),1)
        copied=Catalog(self.base/'migrated/catalog.sqlite3'); copied.database.parent.mkdir()
        with closing(self.catalog.connect()) as original, closing(sqlite3.connect(copied.database)) as backup:
            original.backup(backup)
        result=LocationService(copied).run([row],20,110)
        self.assertEqual(result['recovered'],1,result)
        self.assertAlmostEqual(read_metadata(path,'video')['latitude'],30)
        history=ProcessingHistory(copied).page()[0]
        self.assertNotIn('_recovery',json.loads(history['old_gps_json']))
        self.assertEqual(history['temp_path'],'')

    def test_stop_after_patch_keeps_recoverable_record_and_original_media_payload(self):
        path,row=self.video();signature=integrity(path,threading.Event());cancel=threading.Event()
        def progress(event):
            if event['phase']=='回读定位与校验指纹':cancel.set()
        result=self.service.run([row],30,120,write_mode='fast',cancel=cancel,progress=progress)
        self.assertTrue(result['stopped'])
        self.assertEqual(integrity(path,threading.Event()),signature)
        result=self.service.run([row],20,110)
        self.assertEqual(result['recovered'],1,result)
        self.assertAlmostEqual(read_metadata(path,'video')['latitude'],30)


if __name__ == '__main__': unittest.main()
