import tempfile
import unittest
from pathlib import Path
from PIL import Image
from media_catalog import Catalog
from import_service import ImportService
from watermark_service import WatermarkService


class ImportedQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.source=self.base/'source';self.source.mkdir()
        self.catalog=Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize()
        self.service=ImportService(self.catalog)

    def tearDown(self):self.temp.cleanup()

    def photo(self,name,color='blue'):
        path=self.source/name;Image.new('RGB',(100,80),color).save(path)
        return path

    def rows(self):
        batch=self.catalog.scan(self.source,batch_kind='source_scan',recursive=False)
        return self.catalog.search(batch_id=batch['batch_id'])[1]

    def test_scan_pending_failed_and_historical_are_excluded(self):
        good=self.photo('原片_A.jpg');bad=self.photo('失败_B.jpg','green');pending=self.photo('待处理_C.jpg','red')
        rows=self.rows()
        historical=self.base/'historical';historical.mkdir();Image.new('RGB',(20,30),'yellow').save(historical/'历史.jpg')
        self.catalog.scan(historical)
        identifier=self.service.save_plan(self.service.build_plan(rows,self.source,self.base/'archive'),'copy')
        self.assertEqual(self.catalog.search(imported_only=True)[0],0)
        bad.unlink();pending.unlink()
        result=self.service.run(identifier)
        self.assertEqual(result['complete'],1)
        total,visible=self.catalog.search(imported_only=True)
        self.assertEqual(total,1);self.assertEqual(visible[0]['original_name'],'原片_A.jpg')
        self.assertEqual(self.catalog.search(import_id=identifier,imported_only=True)[0],1)
        self.assertGreater(self.catalog.search()[0],total)
        self.assertTrue(good.exists())

    def test_rescanned_source_search_and_pagination_do_not_leak_unimported_rows(self):
        self.photo('DJI_001.jpg');self.photo('DJI_002.jpg','green')
        rows=self.rows();identifier=self.service.save_plan(self.service.build_plan(rows,self.source,self.base/'archive'),'copy')
        self.assertEqual(self.service.run(identifier)['complete'],2)
        self.rows();self.photo('未导入.jpg','red');self.rows()
        total,page1=self.catalog.search(imported_only=True,limit=1)
        _,page2=self.catalog.search(imported_only=True,limit=1,offset=1)
        self.assertEqual(total,2);self.assertNotEqual(page1[0]['id'],page2[0]['id'])
        total,rows=self.catalog.search(text='DJI_001',kind='photo',gps='missing',imported_only=True)
        self.assertEqual(total,1);self.assertIn('archive',rows[0]['current_path'])
        self.assertEqual(self.catalog.search(text='未导入',imported_only=True)[0],0)

    def test_watermark_export_enters_library_only_after_import(self):
        self.photo('照片.jpg');row=self.rows()[0]
        output=self.base/'watermarked'
        result=WatermarkService(self.catalog).run([row],output,dict(fields=[],custom='我的照片'))
        self.assertEqual(result['complete'],1);self.assertEqual(self.catalog.search(imported_only=True)[0],0)
        exported=next(r for r in self.catalog.search()[1] if r.get('watermark_revision'))
        plan=self.service.build_plan([exported],output,self.base/'archive')
        self.service.run(self.service.save_plan(plan,'copy'))
        total,rows=self.catalog.search(imported_only=True)
        self.assertEqual(total,1);self.assertTrue(rows[0]['watermark_revision'])


if __name__=='__main__':unittest.main()
