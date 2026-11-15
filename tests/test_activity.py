import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from contextlib import closing
from PIL import Image
from activity_service import ActivityService
from media_catalog import Catalog
from import_service import ImportService,digest
from processing_history import ProcessingHistory


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.source=self.base/'source';self.source.mkdir()
        self.catalog=Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize();self.service=ActivityService(self.catalog)
        for i in range(3):Image.new('RGB',(50,40),(20*i,10,30)).save(self.source/f'DJI_{i}.jpg')
        self.catalog.scan(self.source,recursive=False,batch_kind='source_scan');self.rows=self.catalog.search()[1]
    def tearDown(self):self.temp.cleanup()
    def current(self):return self.service.rows([r['id'] for r in self.rows])
    def test_drafts_survive_rescan_restart_import_and_search(self):
        before={r['current_path']:digest(r['current_path']) for r in self.rows}
        self.service.apply(self.rows[:2],new_activity=dict(name='海边亲子游',companions='家人、小王',note='第一次赶海'),patch=dict(place='东疆湾',media_note='日落'))
        event=self.service.activities()[0]
        self.catalog.scan(self.source,recursive=False)
        reopened=Catalog(self.catalog.database);reopened.initialize()
        self.assertEqual(reopened.search(imported_only=True)[0],0)
        self.assertEqual(reopened.search(activity_id=event['id'])[0],2)
        rows=self.current();imports=ImportService(reopened)
        plan=imports.build_plan(rows,self.source,self.base/'archive',dict(place='兜底地点'))
        self.assertEqual([r['place'] for r in plan['items']],['东疆湾','东疆湾','兜底地点'])
        self.assertEqual(imports.run(imports.save_plan(plan,'copy'))['complete'],3)
        for term in ('亲子','小王','赶海','日落','东疆湾'):
            self.assertEqual(reopened.search(text=term,imported_only=True)[0],2,term)
        self.assertEqual(reopened.search(text='%',imported_only=True)[0],0)
        self.assertEqual(reopened.search(activity_id=event['id'],imported_only=True,limit=1)[0],2)
        self.assertEqual({p:digest(p) for p in before},before)
    def test_partial_patch_clear_detach_and_shared_updates(self):
        self.service.apply(self.rows[:2],new_activity=dict(name='出游',companions='小王'),patch=dict(place='海边',media_note='原备注'))
        event=self.service.activities()[0];rows=self.current()
        self.service.apply(rows[:1],activity_id=event['id'],activity_patch=dict(companions='家人'),expected_activity=event,patch=dict(place='餐厅'))
        result=self.catalog.search(activity_id=event['id'])[1]
        self.assertEqual({r['activity_companions'] for r in result},{'家人'})
        self.assertEqual({r['place'] for r in result},{'餐厅','海边'})
        self.assertEqual({r['media_note'] for r in result},{'原备注'})
        self.service.apply(self.current()[:1],activity_id='',patch=dict(place='',media_note=''))
        row=self.current()[0];self.assertIsNone(row['activity_id']);self.assertEqual(row['place'],'')
        plan=ImportService(self.catalog).build_plan([row],self.source,self.base/'archive',dict(place='默认地点'))
        self.assertEqual(plan['items'][0]['place'],'')
        self.assertEqual(len(ProcessingHistory(self.catalog).page(record_type='activity')),3)
    def test_stale_and_invalid_edits_are_atomic(self):
        self.service.apply(self.rows[:1],patch=dict(place='海边'))
        with self.assertRaisesRegex(ValueError,'变化'):
            self.service.apply(self.rows,new_activity=dict(name='不应创建'),patch=dict(media_note='不应保存'))
        self.assertEqual(self.service.activities(),[]);self.assertEqual(self.current()[1]['media_note'],'')
        with self.assertRaises(ValueError):self.service.apply(self.current(),new_activity=dict(name=''))
        self.assertEqual(self.service.activities(),[])
    def test_pending_import_protects_plan_and_draft(self):
        imports=ImportService(self.catalog);imports.save_plan(imports.build_plan(self.rows,self.source,self.base/'archive'),'copy')
        with self.assertRaisesRegex(ValueError,'未完成'):self.service.apply(self.rows,patch=dict(place='不应修改'))
        self.assertTrue(all(r['place'] is None for r in self.current()))

    def test_published_target_indexed_before_recovery_keeps_activity(self):
        self.service.apply(self.rows[:1],new_activity=dict(name='跨批出游'),patch=dict(place='海边',media_note='重要时刻'))
        row=self.current()[0];imports=ImportService(self.catalog)
        identifier=imports.save_plan(imports.build_plan([row],self.source,self.base/'archive'),'copy')
        with patch.object(imports,'_finish_move',side_effect=OSError('publication interrupted')):
            self.assertEqual(imports.run(identifier)['complete'],0)
        self.catalog.scan(self.base/'archive')
        result=imports.run(identifier);self.assertEqual(result['complete'],1,result)
        total,rows=self.catalog.search(imported_only=True,text='重要时刻')
        self.assertEqual(total,1);self.assertEqual(rows[0]['activity_name'],'跨批出游');self.assertEqual(rows[0]['place'],'海边')
    def test_v5_upgrade_backup_is_readable_and_not_repeated(self):
        # A v5-shaped catalog with existing media; no activity data is discarded.
        with closing(self.catalog.connect()) as db,db:
            db.execute('DROP INDEX idx_media_activity')
            db.execute('ALTER TABLE media DROP COLUMN activity_id')
            db.execute('ALTER TABLE media DROP COLUMN media_note')
            db.execute('DROP TABLE activity_changes');db.execute('DROP TABLE activities')
            db.execute('PRAGMA user_version=5')
        self.catalog.initialize();self.catalog.initialize()
        backups=list(self.catalog.database.parent.glob('*.pre-v7-*.backup'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT count(*) FROM media').fetchone()[0],3)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with closing(self.catalog.connect()) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)


if __name__=='__main__':unittest.main()
