import os
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from imageio_ffmpeg import get_ffmpeg_exe
from media_catalog import Catalog,read_metadata
from location_service import LocationService,exiftool
from video_gps import integrity
from import_service import digest,ImportService
from processing_history import ProcessingHistory


class VideoGpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=tempfile.TemporaryDirectory();cls.clip=Path(cls.fixtures.name)/'template.mp4'
        subprocess.run([get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25',
            '-f','lavfi','-i','sine=frequency=440','-t','0.4','-c:v','libx264','-c:a','aac','-movflags','+faststart',
            '-metadata','creation_time=2026-11-09T01:04:32Z',str(cls.clip)],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        cls.mov=cls.clip.with_suffix('.mov')
        subprocess.run([get_ffmpeg_exe(),'-v','error','-i',str(cls.clip),'-c','copy','-timecode','01:00:00:00',str(cls.mov)],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    @classmethod
    def tearDownClass(cls):cls.fixtures.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.source=self.base/'source';self.source.mkdir()
        self.catalog=Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize();self.service=LocationService(self.catalog)
    def tearDown(self):self.temp.cleanup()
    def video(self,name='相机视频.mp4'):
        path=self.source/name;shutil.copy2(self.mov if path.suffix=='.mov' else self.clip,path);self.catalog.scan(self.source,recursive=False)
        return path,self.catalog.search(text=name)[1][0]
    def test_mp4_mov_write_readback_preserve_payload_and_count(self):
        for name in ('相机.mp4','相机.mov'):
            path,row=self.video(name);before=integrity(path,threading.Event());stamp=path.stat().st_mtime_ns
            result=self.service.run([row],-33.8568,151.2153,location_name='海边');self.assertEqual(result['complete'],1,result)
            self.assertEqual(integrity(path,threading.Event()),before);self.assertEqual(path.stat().st_mtime_ns,stamp)
            meta=read_metadata(path,'video');self.assertEqual(meta['gps_status'],'present');self.assertAlmostEqual(meta['latitude'],-33.8568,places=6)
            self.catalog.scan(self.source,recursive=False,retry_metadata=True)
            self.assertEqual(self.catalog.search(text=name)[1][0]['gps_status'],'present')
        records=ProcessingHistory(self.catalog).page();self.assertEqual(sum(r['completed_videos'] for r in records),2);self.assertEqual(sum(r['completed_photos'] for r in records),0)
    def test_default_skip_and_explicit_replace(self):
        path,row=self.video();self.assertEqual(self.service.run([row],30,120)['complete'],1)
        row=self.catalog.search()[1][0];before=digest(path)
        self.assertEqual(self.service.run([row],20,110)['skipped'],1);self.assertEqual(digest(path),before)
        result=self.service.run([row],20,110,overwrite=True);self.assertEqual(result['complete'],1,result)
        self.assertAlmostEqual(read_metadata(path,'video')['latitude'],20)
        row=self.catalog.search()[1][0]
        result=self.service.run([row],0.0000001,-0.0000001,overwrite=True);self.assertEqual(result['complete'],1,result)
    def test_corrupt_payload_validation_preserves_original(self):
        path,row=self.video();before=digest(path)
        def corrupt(args,cancel=None,**kwargs):
            result=exiftool(args,cancel,**kwargs)
            if '-o' in args:
                target=Path(args[args.index('-o')+1]);data=bytearray(target.read_bytes());index=data.index(b'mdat')+9;data[index]^=1;target.write_bytes(data)
            return result
        with patch('location_service.exiftool',side_effect=corrupt):result=self.service.run([row],30,120)
        self.assertEqual(len(result['errors']),1);self.assertEqual(digest(path),before);self.assertFalse(list(self.source.glob('.mediaorganizer-*')))
    def test_ready_recovery_on_both_sides_of_replace(self):
        for before_replace in (True,False):
            path,row=self.video(f'recovery{before_replace}.mp4')
            guard=patch('location_service.os.replace',side_effect=OSError('interrupted')) if before_replace else patch.object(self.service,'_finish',side_effect=OSError('interrupted'))
            with guard:self.assertEqual(len(self.service.run([row],30,120)['errors']),1)
            with self.assertRaises(ValueError):ImportService(self.catalog).build_plan([row],self.source,self.base/'archive')
            with patch('video_gps.write_verified',side_effect=AssertionError('must not rewrite')):
                result=self.service.run([row],20,110)
            self.assertEqual(result['recovered'],1,result);self.assertAlmostEqual(read_metadata(path,'video')['latitude'],30)
    def test_unknown_private_atoms_preserved_and_cancel_no_write(self):
        path,row=self.video();data=path.read_bytes();payload=b'private gyro telemetry 123456';path.write_bytes(data+struct.pack('>I4s',len(payload)+8,b'zzzz')+payload)
        self.catalog.scan(self.source,recursive=False);row=self.catalog.search()[1][0];before=digest(path)
        cancel=threading.Event();cancel.set();self.assertTrue(self.service.run([row],30,120,cancel=cancel)['stopped']);self.assertEqual(digest(path),before)
        signature=integrity(path,threading.Event());result=self.service.run([row],30,120)
        self.assertEqual(result['complete'],1,result);self.assertEqual(integrity(path,threading.Event()),signature)
    def test_unsupported_fragmented_layout_is_not_published(self):
        path,row=self.video();path.write_bytes(path.read_bytes()+struct.pack('>I4s',8,b'moof'))
        self.catalog.scan(self.source,recursive=False);row=self.catalog.search()[1][0];before=digest(path)
        self.assertEqual(len(self.service.run([row],30,120)['errors']),1);self.assertEqual(digest(path),before)


if __name__=='__main__':unittest.main()
