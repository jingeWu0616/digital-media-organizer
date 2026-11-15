import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image
from color_service import ColorService
from fast_video_gps import (box, commit, gps_value, plan_value, read_moov, Unsupported)
from import_service import digest, Cancelled
from location_service import exiftool
from media_catalog import Catalog
from task_queue import TaskQueue, can_use_color_gps_compatible
from video_gps import integrity, metadata, preserve_keys_after_color, GPS_COMPATIBILITY_REQUIRED


class ColorGpsFastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=tempfile.TemporaryDirectory()
        root=Path(cls.fixtures.name); cls.template=root/'tail.mp4'
        cover=root/'cover.jpg';Image.new('RGB',(64,36),'orange').save(cover)
        subprocess.run([get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25',
            '-f','lavfi','-i','sine=frequency=440','-i',str(cover),'-map','0:v','-map','1:a','-map','2:v',
            '-t','0.4','-c:v:0','libx265','-pix_fmt:v:0','yuv420p10le','-x265-params','pools=2',
            '-c:a','aac','-c:v:1','copy','-disposition:v:1','attached_pic',
            '-metadata','comment=original note','-metadata','creation_time=2026-11-09T01:04:32Z',str(cls.template)],
            check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)

    @classmethod
    def tearDownClass(cls): cls.fixtures.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)/'media';self.base.mkdir()
        self.source=self.base/'original.mp4';shutil.copy2(self.template,self.source)
        self.output=self.base/'.mediaorganizer-color-test.part';shutil.copy2(self.template,self.output)
        self.cancel=threading.Event()
        self.value=b'+30.123456789012+120.987654321098-0012.3456/'
        self.put_gps(self.source,self.value)
        self.original_hash=digest(self.source)

    def tearDown(self): self.temp.cleanup()

    def put_gps(self,path,value):
        with path.open('r+b') as stream:
            offset,_,after=plan_value(stream,value);commit(stream,offset,after)

    def raw_gps(self,path):
        with path.open('rb') as stream:return gps_value(read_moov(stream)[1])

    def test_exact_gps_altitude_cover_audio_and_small_write(self):
        signature=integrity(self.output,self.cancel)
        with self.output.open('rb') as stream:offset,_=read_moov(stream)
        prefix=self.output.read_bytes()[:offset];writes=[]
        def spy(stream,start,after):
            writes.append((start,len(after)));return commit(stream,start,after)
        def read_only_tool(args,*a,**kw):
            self.assertNotIn('-tagsFromFile',args);self.assertNotIn('-overwrite_original',args)
            return exiftool(args,*a,**kw)
        with patch('fast_video_gps.commit',side_effect=spy),patch('location_service.exiftool',side_effect=read_only_tool):
            preserve_keys_after_color(self.source,self.output,self.cancel)
        self.assertEqual(self.raw_gps(self.output),self.value)
        self.assertEqual(metadata(self.output,exiftool,self.cancel)['Keys:GPSCoordinates'],
                         metadata(self.source,exiftool,self.cancel)['Keys:GPSCoordinates'])
        self.assertEqual(integrity(self.output,self.cancel),signature)
        self.assertEqual(self.output.read_bytes()[:offset],prefix)
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][0],offset)
        self.assertLess(writes[0][1],self.output.stat().st_size)
        self.assertEqual(digest(self.source),self.original_hash)

    def test_already_identical_gps_does_not_write(self):
        self.put_gps(self.output,self.value);before=digest(self.output)
        with patch('fast_video_gps.commit',side_effect=AssertionError('must skip')):
            preserve_keys_after_color(self.source,self.output,self.cancel)
        self.assertEqual(digest(self.output),before)

    def test_no_gps_source_skips_all_writes(self):
        source=self.base/'no-gps.mp4';shutil.copy2(self.template,source)
        before=digest(self.output)
        with patch('fast_video_gps.commit',side_effect=AssertionError('must skip')):
            preserve_keys_after_color(source,self.output,self.cancel)
        self.assertEqual(digest(self.output),before)

    def test_front_metadata_fails_before_writing_and_explicit_compatibility(self):
        front=self.base/'front.mp4'
        subprocess.run([get_ffmpeg_exe(),'-v','error','-i',str(self.template),'-map','0','-c','copy',
            '-movflags','+faststart',str(front)],check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        shutil.copy2(front,self.output);before=digest(self.output)
        with self.assertRaisesRegex(ValueError,GPS_COMPATIBILITY_REQUIRED):
            preserve_keys_after_color(self.source,self.output,self.cancel)
        self.assertEqual(digest(self.output),before)
        preserve_keys_after_color(self.source,self.output,self.cancel,mode='compatible')
        self.assertEqual(metadata(self.output,exiftool,self.cancel)['Keys:GPSCoordinates'],
                         metadata(self.source,exiftool,self.cancel)['Keys:GPSCoordinates'])
        self.assertEqual(digest(self.source),self.original_hash)

    def test_cancel_before_patch_keeps_files(self):
        before=digest(self.output);self.cancel.set()
        with self.assertRaises(Cancelled):preserve_keys_after_color(self.source,self.output,self.cancel)
        self.assertEqual(digest(self.output),before);self.assertEqual(digest(self.source),self.original_hash)

    def catalog(self):
        catalog=Catalog(self.base.parent/'data/catalog.sqlite3');catalog.initialize();catalog.scan(self.base)
        row=next(r for r in catalog.search()[1] if r['current_path']==str(self.source))
        return catalog,row

    def test_corrupted_payload_is_never_published(self):
        catalog,row=self.catalog()
        def corrupt(stream,offset,after):
            commit(stream,offset,after)
            stream.seek(0);data=stream.read(offset);pos=data.index(b'mdat')+8
            stream.seek(pos);stream.write(bytes([data[pos]^1]));os.fsync(stream.fileno())
        with patch('fast_video_gps.commit',side_effect=corrupt):
            result=ColorService(catalog).run([row],self.base,confirmed_dlogm=True,mode='cpu')
        self.assertEqual(len(result['errors']),1,result);self.assertEqual(digest(self.source),self.original_hash)
        with closing(catalog.connect()) as db:self.assertEqual(db.execute('SELECT status FROM color_jobs').fetchone()[0],'failed')

    def test_roundtrip_precision_equator_and_negative_altitude(self):
        for value in (b'+00.000000000001-000.000000000001-0012.3456/',b'-33.8568001234+151.2153001234+1234.5678/'):
            self.put_gps(self.source,value)
            preserve_keys_after_color(self.source,self.output,self.cancel)
            self.assertEqual(self.raw_gps(self.output),value)

    def test_color_replace_and_copy_keep_precise_gps(self):
        catalog,row=self.catalog()
        result=ColorService(catalog).run([row],self.base,confirmed_dlogm=True,mode='cpu',output_mode='copy')
        self.assertEqual(result['complete'],1,result);self.assertEqual(digest(self.source),self.original_hash)
        self.assertEqual(self.raw_gps(self.source.with_stem('original_Rec709')),self.value)
        result=ColorService(catalog).run([row],self.base,confirmed_dlogm=True,mode='cpu',output_mode='replace')
        self.assertEqual(result['complete'],1,result);self.assertEqual(self.raw_gps(self.source),self.value)

    def test_compatibility_retry_is_explicit_and_preserves_attempts(self):
        catalog,row=self.catalog();q=TaskQueue(catalog)
        expected=metadata(self.source,exiftool,self.cancel)['Keys:GPSCoordinates']
        def wait():
            deadline=time.monotonic()+30
            while q.active and time.monotonic()<deadline:q.tick();time.sleep(.01)
            self.assertFalse(q.active)
        try:
            q.submit('color',[row],dict(source_root=str(self.base),confirmed_dlogm=True,mode='cpu'))
            with patch('fast_video_gps.plan_value',side_effect=Unsupported('unsupported test layout')):wait()
            old=q.snapshot()[0];self.assertTrue(can_use_color_gps_compatible(old));self.assertEqual(digest(self.source),self.original_hash)
            self.assertNotIn('gps_preservation',json.loads(old['options_json']))
            q.retry(old['id'],compatible_gps=True);wait()
            new=q.snapshot()[-1];self.assertEqual(new['status'],'complete',new)
            self.assertEqual(new['retry_of'],old['id']);self.assertEqual(q.snapshot()[0]['status'],'failed')
            self.assertEqual(json.loads(new['options_json'])['gps_preservation'],'compatible')
            self.assertEqual(metadata(self.source,exiftool,self.cancel)['Keys:GPSCoordinates'],expected)
        finally:q.shutdown()


if __name__=='__main__':unittest.main()
