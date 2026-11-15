import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, closing
from unittest.mock import patch

from PIL import Image
from imageio_ffmpeg import get_ffmpeg_exe
import color_service as cs
import media_catalog as mc
from import_service import digest, Cancelled
from location_service import LocationService, exiftool
from processing_markers import COLOR, WATERMARK
from task_queue import TaskQueue
from video_gps import metadata
from watermark_service import WatermarkService


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=tempfile.TemporaryDirectory()
        cls.clip=Path(cls.fixture.name)/'sample.mp4'
        subprocess.run([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi','-i',
                        'testsrc2=size=320x180:rate=25','-t','1','-c:v','libx264',
                        '-metadata','comment=原备注 test',str(cls.clip)],check=True,capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)

    @classmethod
    def tearDownClass(cls):cls.fixture.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.source=self.base/'media';self.source.mkdir()
        self.video=self.source/'DJI_001.mp4';shutil.copy2(self.clip,self.video)
        self.catalog=mc.Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize();self.catalog.scan(self.source)
        self.row=self.catalog.search()[1][0];self.queue=None

    def tearDown(self):
        if self.queue:self.queue.control(None,'stop');self.drain();self.queue.shutdown()
        self.temp.cleanup()

    def drain(self):
        end=time.monotonic()+20
        while self.queue.active and time.monotonic()<end:self.queue.tick();time.sleep(.01)
        self.assertFalse(self.queue.active)

    def test_real_progress_arrives_before_ffmpeg_exits(self):
        events=[];start=time.monotonic()
        cs.command(['-re','-f','lavfi','-i','testsrc2=size=64x64:rate=10','-t','1.5','-f','null','-'],
                   threading.Event(),frame_progress=lambda frame:events.append((time.monotonic(),frame)))
        end=time.monotonic()
        self.assertTrue(any(0<f<15 and stamp<end-.2 for stamp,f in events),events)
        self.assertLess(end-start,8)

    def test_streaming_progress_can_cancel(self):
        cancel=threading.Event();timer=threading.Timer(.4,cancel.set);timer.start();start=time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                cs.command(['-re','-f','lavfi','-i','testsrc2=size=64x64:rate=10','-t','20','-f','null','-'],cancel,frame_progress=lambda _:None)
        finally:timer.cancel()
        self.assertLess(time.monotonic()-start,5)

    def test_write_failure_does_not_reencode_on_cpu(self):
        service=cs.ColorService(self.catalog);calls=[]
        def fail(args,*a,**kw):calls.append(args);raise RuntimeError('Error opening output: Permission denied (qsv)')
        with patch.object(service,'_encoder',return_value=('h264_qsv',[],'nv12')) as encoder,patch.object(cs,'command',side_effect=fail):
            result=service.run([self.row],self.source,confirmed_dlogm=True)
        self.assertEqual(encoder.call_count,1);self.assertEqual(len(calls),1);self.assertTrue(result['errors'])
        self.assertEqual(digest(self.video),digest(self.clip))

    def test_amf_probe_uses_real_parameters_and_caches(self):
        service=cs.ColorService(self.catalog)
        spec=dict(format='HEVC',bit_depth=10,width=3840,height=2160,frame_rate='50.000')
        calls=[]
        def probe(args,*a,**kw):
            calls.append(args)
            if 'hevc_amf' not in args:raise RuntimeError('hardware unavailable')
        with patch.object(cs,'command',side_effect=probe),patch.object(cs,'profile',return_value=spec):
            first=service._encoder(spec,'auto',threading.Event());second=service._encoder(spec,'auto',threading.Event())
        self.assertEqual(first[0],'hevc_amf');self.assertEqual(first,second);self.assertEqual(len(calls),3)
        self.assertIn('color=size=3840x2160:rate=50.000',calls[-1]);self.assertIn('main10',calls[-1]);self.assertIn('p010le',calls[-1])

    def test_amf_bit_depth_downgrade_rejected(self):
        service=cs.ColorService(self.catalog);spec=dict(format='HEVC',bit_depth=10,width=3840,height=2160,frame_rate='50')
        with patch.object(cs,'command'),patch.object(cs,'profile',return_value=dict(spec,bit_depth=8)):
            self.assertEqual(service._encoder(spec,'auto',threading.Event())[0],'libx265')

    def test_color_marker_survives_new_catalog_and_gps(self):
        result=cs.ColorService(self.catalog).run([self.row],self.source,confirmed_dlogm=True,mode='cpu')
        self.assertEqual(result['complete'],1,result)
        self.assertIn('原备注 test',cs.profile(self.video)['comment']);self.assertIn(COLOR,cs.profile(self.video)['comment'])
        row=self.catalog.search()[1][0]
        self.assertEqual(LocationService(self.catalog).run([row],30,120)['complete'],1)
        self.assertIn(COLOR,cs.profile(self.video)['comment'])
        shutil.copy2(self.clip,self.video.with_suffix('.LRF'))
        fresh=mc.Catalog(self.base/'fresh/catalog.sqlite3');fresh.initialize();fresh.scan(self.source)
        service=cs.ColorService(fresh);self.assertEqual(service.auto_candidates(fresh.search()[1])[0],[])
        row=fresh.search(kind='video')[1];before=digest(self.video)
        result=service.run(row,self.source,confirmed_dlogm=True,mode='cpu')
        self.assertEqual(result['complete'],0);self.assertEqual(digest(self.video),before)

    def test_watermark_marker_survives_new_catalog(self):
        photo=self.source/'photo.jpg';exif=Image.Exif();exif[270]='original description'
        Image.new('RGB',(600,400),'blue').save(photo,exif=exif)
        self.catalog.scan(self.source);row=self.catalog.search(kind='photo')[1][0]
        service=WatermarkService(self.catalog);target=self.base/'output'
        result=service.run([row],target,dict(custom='test',fields=[]))
        self.assertEqual(result['complete'],1,result)
        fresh=mc.Catalog(self.base/'fresh/catalog.sqlite3');fresh.initialize();fresh.scan(target)
        row=fresh.search()[1][0];self.assertTrue(row['watermark_revision'])
        with Image.open(row['current_path']) as image:
            self.assertIn(WATERMARK,image.getexif()[270]);self.assertIn('original description',image.getexif()[270])
        result=WatermarkService(fresh).run([row],self.base/'second',dict(custom='again',fields=[]))
        self.assertEqual(result['skipped'],1)

    def test_scan_error_releases_database_before_slow_read(self):
        media=self.base/'scan';media.mkdir()
        for i in range(3):Image.new('RGB',(10,10)).save(media/f'{i}.jpg')
        entered=threading.Event();release=threading.Event();calls=[0];errors=[];original=mc.read_metadata
        @contextmanager
        def guard(path):calls[0]+=1;yield calls[0]!=2
        def slow(path,kind):
            if calls[0]==3:entered.set();release.wait(3)
            return original(path,kind)
        def scan():
            try:self.catalog.scan(media,file_guard=guard)
            except Exception as exc:errors.append(exc)
        with patch.object(mc,'read_metadata',side_effect=slow):
            thread=threading.Thread(target=scan);thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with closing(sqlite3.connect(self.catalog.database,timeout=.15)) as db:
                    db.execute('UPDATE batches SET status=status');db.commit()
            finally:release.set();thread.join(5)
        self.assertEqual(errors,[])

    def test_explicit_follow_output_writes_copy_only(self):
        q=self.queue=TaskQueue(self.catalog);before=digest(self.video)
        q.submit('color',[self.row],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu',output_mode='copy'))
        q.submit('gps',[self.row],dict(latitude=30.,longitude=120.),follow_outputs=q.output_predecessors([self.row]))
        self.drain();self.assertEqual([r['status'] for r in q.snapshot()],['complete','complete'])
        self.assertEqual(digest(self.video),before)
        copy=self.source/'DJI_001_Rec709.mp4'
        self.assertEqual(metadata(copy,exiftool,threading.Event())['Keys:GPSCoordinates'],'30 120')
        self.assertEqual(Path(q.snapshot()[1]['source_path']).resolve(),copy.resolve())

    def test_failed_predecessor_does_not_fall_back_to_original(self):
        called=[]
        def run(action,*args):called.append(action);return dict(errors=['test failure'])
        q=self.queue=TaskQueue(self.catalog,runner=run)
        q.submit('color',[self.row],dict(output_mode='copy'))
        q.submit('gps',[self.row],dict(latitude=30.,longitude=120.),follow_outputs=q.output_predecessors([self.row]))
        self.drain();self.assertEqual(called,['color']);self.assertEqual(q.snapshot()[1]['status'],'failed')

    def test_nested_readers_do_not_release_each_other(self):
        q=self.queue=TaskQueue(self.catalog)
        with q.read_guard(self.video):
            outer=set(q.readers)
            with q.read_guard(self.video):pass
            self.assertEqual(set(q.readers),outer)
        self.assertFalse(q.readers)

    def test_watermark_keeps_chinese_multiline_description(self):
        from watermark_service import copy_metadata, photo_values
        source=self.source/'chinese.jpg';target=self.source/'result.jpg'
        Image.new('RGB',(100,100)).save(source);Image.new('RGB',(100,100)).save(target)
        note=self.base/'note.txt';note.write_bytes('中文备注\n第二行'.encode('utf-8'))
        exiftool(['-overwrite_original','-EXIF:ImageDescription<='+str(note),str(source)])
        copy_metadata(source,target,(100,100),threading.Event())
        self.assertEqual(photo_values(target)[270],'中文备注\n第二行 | '+WATERMARK)

    def test_modified_output_is_rejected_before_follow_gps(self):
        q=self.queue=TaskQueue(self.catalog)
        q.submit('color',[self.row],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu',output_mode='copy'))
        self.drain();copy=self.source/'DJI_001_Rec709.mp4';original=digest(self.video)
        q.submit('gps',[self.row],dict(latitude=30.,longitude=120.),follow_outputs=q.output_predecessors([self.row]))
        with copy.open('ab') as stream:stream.write(b'outside change')
        self.drain();self.assertEqual(q.snapshot()[-1]['status'],'failed')
        self.assertIn('已变化',q.snapshot()[-1]['error']);self.assertEqual(digest(self.video),original)

    def test_follow_can_retry_after_restart_without_auto_execution(self):
        q=self.queue=TaskQueue(self.catalog)
        q.submit('color',[self.row],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu',output_mode='copy'))
        self.drain()
        q.submit('gps',[self.row],dict(latitude=30.,longitude=120.),follow_outputs=q.output_predecessors([self.row]))
        identifier=q.snapshot()[-1]['id'];q.shutdown()
        self.queue=TaskQueue(self.catalog);self.assertFalse(self.queue.active)
        self.queue.retry(identifier);self.drain()
        self.assertEqual(self.queue.snapshot()[-1]['status'],'complete')
        self.assertEqual(digest(self.video),digest(self.clip))
