import json
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from PIL import Image
from media_catalog import Catalog
from task_queue import TaskQueue
from processing_history import ProcessingHistory


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.source=self.base/'source';self.source.mkdir()
        for i in range(4):Image.new('RGB',(30,20),(i*30,50,90)).save(self.source/f'{i}.jpg')
        self.catalog=Catalog(self.base/'data'/'db.sqlite3');self.catalog.initialize()
        self.catalog.scan(self.source)
        self.rows=self.catalog.search()[1]
        self.queue=None;self.gates=[]

    def tearDown(self):
        for gate in self.gates:gate.set()
        if self.queue:
            self.queue.control(None,'stop');self.drain();self.queue.shutdown()
        self.temp.cleanup()

    def drain(self, seconds=15):
        end=time.monotonic()+seconds
        while self.queue.active and time.monotonic()<end:
            self.queue.tick();time.sleep(.01)
        self.assertFalse(self.queue.active)

    def gate(self):
        event=threading.Event();self.gates.append(event);return event

    def test_limits_order_pause_and_durable_timings(self):
        gate=self.gate();started=[]
        def run(action,row,options,cancel,progress):
            started.append((action,row['id']))
            progress(dict(phase='实际处理',percent=25))
            gate.wait(3)
            progress(dict(phase='回读校验',percent=None))
            return dict(complete=1)
        q=self.queue=TaskQueue(self.catalog,runner=run)
        first=q.submit('color',[self.rows[0],self.rows[1]],{})
        q.submit('gps',[self.rows[0],self.rows[2]],{})
        q.tick();time.sleep(.06)
        self.assertEqual(len(started),2)
        self.assertIn(('color',self.rows[0]['id']),started)
        self.assertIn(('gps',self.rows[2]['id']),started)
        q.control(first,'pause');gate.set()
        for _ in range(20):q.tick();time.sleep(.01)
        self.assertNotIn(('color',self.rows[1]['id']),started)
        q.control(first,'resume');self.drain()
        self.assertEqual(len(started),4)
        with closing(self.catalog.connect()) as db:
            rows=db.execute('SELECT * FROM task_actions').fetchall()
        self.assertTrue(all(r['started_at'] and r['completed_at'] and r['elapsed']>0 for r in rows))
        self.assertTrue(any(r['pause_seconds']>0 for r in rows))
        self.assertTrue(any('回读校验' in r['phases_json'] for r in rows))

    def test_cancel_retry_and_restart_never_autoruns(self):
        q=self.queue=TaskQueue(self.catalog,runner=lambda *args:dict(errors=['拒绝写入']))
        q.submit('gps',[self.rows[0]],dict(latitude=1,longitude=2))
        self.drain();failed=q.snapshot()[0]
        q.runner=lambda *args:dict(complete=1)
        q.retry(failed['id']);self.drain()
        self.assertEqual(len(ProcessingHistory(self.catalog).page(record_type='gps')),2)
        q.submit('gps',[self.rows[1]],dict(latitude=1,longitude=2))
        q.shutdown()
        # Simulate a power loss after durable enqueue, before execution.
        with closing(self.catalog.connect()) as db,db:db.execute("UPDATE task_actions SET status='running' WHERE media_id=?",(self.rows[1]['id'],))
        self.queue=TaskQueue(self.catalog)
        with closing(self.catalog.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM task_actions WHERE media_id=?',(self.rows[1]['id'],)).fetchone()[0],'interrupted')
        self.assertFalse(self.queue.active)

    def test_same_file_success_uses_updated_snapshot(self):
        seen=[]
        def run(action,row,options,cancel,progress):
            seen.append(row['size'])
            if len(seen)==1:
                with closing(self.catalog.connect()) as db,db:db.execute('UPDATE media SET size=size+1 WHERE id=?',(row['id'],))
            return dict(complete=1)
        q=self.queue=TaskQueue(self.catalog,runner=run)
        q.submit('gps',[self.rows[0]],{});q.submit('color',[self.rows[0]],{})
        self.drain();self.assertEqual(seen,[self.rows[0]['size'],self.rows[0]['size']+1])
        q.submit('gps',[self.rows[0]],{})
        self.drain();self.assertEqual(seen[-1],self.rows[0]['size']+1)

    def test_external_change_is_not_blessed(self):
        q=self.queue=TaskQueue(self.catalog)
        q.submit('gps',[self.rows[0]],dict(latitude=39.9,longitude=116.4))
        Path(self.rows[0]['current_path']).write_bytes(b'outside edit')
        self.drain();self.assertEqual(q.snapshot()[0]['status'],'failed')
        self.assertEqual(Path(self.rows[0]['current_path']).read_bytes(),b'outside edit')

    def test_real_gps_then_watermark_and_unified_history(self):
        q=self.queue=TaskQueue(self.catalog)
        q.submit('gps',[self.rows[0]],dict(latitude=39.9,longitude=116.4))
        self.drain()
        self.assertEqual(q.snapshot()[0]['status'],'complete',q.snapshot())
        history=ProcessingHistory(self.catalog).page(record_type='gps')
        self.assertEqual(len(history),1);self.assertEqual(history[0]['record_type'],'task')
        self.assertTrue(json.loads(history[0]['phases_json']))
        with closing(self.catalog.connect()) as db:
            self.assertAlmostEqual(db.execute('SELECT latitude FROM media WHERE id=?',(self.rows[0]['id'],)).fetchone()[0],39.9)

    def test_import_preview_conflict_and_real_copy_timing(self):
        from import_service import ImportService
        q=self.queue=TaskQueue(self.catalog)
        service=ImportService(self.catalog)
        plan=service.build_plan(self.rows[:2],self.source,self.base/'archive')
        identifier=service.save_plan(plan,'copy');items=service.items(identifier)
        q.submit('import',self.rows[:2],dict(import_id=identifier,items={r['media_id']:r['id'] for r in items}))
        with self.assertRaises(ValueError):q.submit('gps',[self.rows[0]],{})
        self.drain()
        self.assertTrue(all(r['status']=='complete' for r in q.snapshot()),q.snapshot())
        self.assertEqual(len(ProcessingHistory(self.catalog).timings('import',identifier)),2)
        self.assertEqual(service.history()[0]['status'],'completed')
        self.assertTrue(all(Path(r['current_path']).exists() for r in self.rows))

    def test_scan_does_not_race_current_file(self):
        gate=self.gate()
        q=self.queue=TaskQueue(self.catalog,runner=lambda *args:(gate.wait(3) or dict(complete=1)))
        # Use an explicit result: Event.wait returns bool.
        def run(*args):gate.wait(3);return dict(complete=1)
        q.runner=run
        q.submit('gps',[self.rows[0]],{});q.tick()
        result=self.catalog.scan(self.source,file_guard=q.read_guard)
        self.assertEqual(result['errors'],1)
        self.assertEqual(result['photos'],3)
        gate.set();self.drain()

    def test_actual_color_then_gps_with_parallel_photo(self):
        import subprocess,os
        from imageio_ffmpeg import get_ffmpeg_exe
        path=self.source/'DJI_001.mp4'
        subprocess.run([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi','-i',
                        'testsrc2=size=64x64:rate=5','-t','0.4','-c:v','libx264',str(path)],
                       check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.catalog.scan(self.source);video=self.catalog.search(kind='video')[1][0]
        q=self.queue=TaskQueue(self.catalog)
        q.submit('color',[video],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu'))
        q.submit('gps',[video,self.rows[0]],dict(latitude=30.,longitude=120.))
        self.drain(30)
        self.assertTrue(all(r['status']=='complete' for r in q.snapshot()),q.snapshot())
        row=self.catalog.search(kind='video')[1][0]
        self.assertAlmostEqual(row['latitude'],30.);self.assertTrue(row['color_revision'])
        q.submit('color',[row],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu'))
        self.drain();self.assertEqual(q.snapshot()[-1]['status'],'skipped')

    def test_paused_predecessor_cannot_be_overtaken_or_count_as_execution(self):
        started=[]
        q=self.queue=TaskQueue(self.catalog,runner=lambda action,*args:(started.append(action) or dict(complete=1)))
        batch=q.submit('gps',[self.rows[0]],{})
        q.control(batch,'pause');q.submit('color',[self.rows[0]],{})
        for _ in range(5):q.tick();time.sleep(.01)
        self.assertEqual(started,[])
        q.control(batch,'resume');self.drain()
        self.assertEqual(started,['gps','color'])
        first=q.snapshot()[0];self.assertGreater(first['pause_seconds'],.04)
        self.assertLess(first['elapsed'],first['pause_seconds'])

    def test_v6_migration_backup_preserves_history(self):
        import sqlite3
        with closing(self.catalog.connect()) as db,db:
            db.execute('DROP TABLE task_actions');db.execute('PRAGMA user_version=6')
        self.catalog.initialize()
        copies=list(self.catalog.database.parent.glob('*.pre-v7-*.backup'))
        self.assertEqual(len(copies),1)
        with closing(sqlite3.connect(copies[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertEqual(db.execute('SELECT count(*) FROM media').fetchone()[0],4)

    def test_logo_changed_while_waiting_requires_new_preview(self):
        logo=self.base/'logo.png';Image.new('RGB',(20,20),'red').save(logo)
        q=self.queue=TaskQueue(self.catalog)
        q.submit('watermark',[self.rows[0]],dict(target_root=str(self.base/'exports'),options=dict(logo=str(logo))))
        Image.new('RGB',(20,20),'green').save(logo)
        self.drain();record=q.snapshot()[0]
        self.assertEqual(record['status'],'failed')
        self.assertIn('Logo 已变化',record['error'])
        self.assertFalse((self.base/'exports').exists())

    def test_recovery_records_actual_old_coordinates_not_new_request(self):
        from unittest.mock import patch
        from location_service import LocationService
        q=self.queue=TaskQueue(self.catalog)
        with patch.object(LocationService,'_finish',side_effect=OSError('模拟保存后中断')):
            q.submit('gps',[self.rows[0]],dict(latitude=30.,longitude=120.))
            self.drain()
        self.assertEqual(q.snapshot()[0]['status'],'failed')
        q.submit('gps',[self.rows[0]],dict(latitude=31.,longitude=121.))
        self.drain();last=q.snapshot()[-1]
        self.assertEqual(last['status'],'complete');self.assertIn('新坐标未应用',last['error'])
        details=ProcessingHistory(self.catalog).details('task',last['id'])[0]
        self.assertAlmostEqual(details['journal']['latitude'],30.)

    def test_stopped_import_keeps_per_file_timing_and_finish_time(self):
        from import_service import ImportService
        service=ImportService(self.catalog);q=self.queue=TaskQueue(self.catalog)
        identifier=service.save_plan(service.build_plan(self.rows,self.source,self.base/'archive'),'copy')
        items=service.items(identifier)
        batch=q.submit('import',self.rows,dict(import_id=identifier,items={i['media_id']:i['id'] for i in items}))
        q.control(batch,'stop');self.assertFalse(q.active)
        before=service.history()[0]['finished_at']
        self.assertEqual(service.history()[0]['status'],'stopped')
        self.assertEqual(len(ProcessingHistory(self.catalog).timings('import',identifier)),4)
        time.sleep(.02);q.shutdown()
        self.assertEqual(service.history()[0]['finished_at'],before)


if __name__=='__main__':unittest.main()
