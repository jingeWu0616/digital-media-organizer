import json
import hashlib
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops
from media_catalog import Catalog, read_metadata
from import_service import ImportService, digest
from location_service import LocationService
from processing_history import ProcessingHistory
from watermark_service import WatermarkService, DEFAULTS, caption, normalize, read_photo, render


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve()
        self.source=self.base/'原始照片';self.source.mkdir();self.target=self.base/'水印照片'
        self.catalog=Catalog(self.base/'data/catalog.sqlite3');self.catalog.initialize()
        self.service=WatermarkService(self.catalog)
        self.history=ProcessingHistory(self.catalog)

    def tearDown(self):self.temp.cleanup()

    def photo(self,name='旅行照片.jpg',orientation=1):
        path=self.source/name
        exif=Image.Exif();exif[271]='Camera';exif[272]='Test Model';exif[274]=orientation
        exif[34665]={36867:'2025:03:04 05:06:07',33437:2.8,33434:.01,34855:200,37386:35.0}
        image=Image.new('RGB',(1200,800),'#315d83')
        image.paste('#e7b46b',(0,0,300,400))
        color=tuple(hashlib.sha256(name.encode()).digest()[:3]);image.paste(color,(500,300,600,400))
        image.save(path,format='HEIF' if path.suffix=='.heic' else None,exif=exif.tobytes())
        self.catalog.scan(self.source)
        row=next(r for r in self.catalog.search()[1] if Path(r['current_path']).resolve()==path)
        return path,row

    def export(self,row,**options):
        return self.service.run([row],self.target,dict(custom='我的旅途',**options))

    def test_three_styles_original_pixels_dimensions_names_and_metadata(self):
        path,row=self.photo();fingerprint=digest(path)
        for style in ('corner','bar','frame'):
            with self.subTest(style=style):
                result=self.export(row,style=style)
                self.assertEqual(result['errors'],[])
                job=self.history.page(record_type='watermark')[0]
                output=Path(job['output_path'])
                with Image.open(output) as image:
                    self.assertEqual(image.width,1200)
                    self.assertEqual(image.height==800,style!='frame')
                    self.assertEqual(image.getexif()[274],1)
                    self.assertEqual(image.getexif()[272],'Test Model')
                    self.assertEqual(image.getexif().get_ifd(34665)[36867],'2025:03:04 05:06:07')
                self.assertEqual(digest(path),fingerprint)
                indexed=self.history.media(job['output_media_id'])
                self.assertEqual(indexed['original_name'],path.name)
                self.assertEqual(indexed['watermark_revision'],digest(output))
                self.assertEqual(job['type_label'],'照片水印')
        self.assertEqual(len(list(self.target.glob('*.jpg'))),3)

    def test_supported_formats_and_exif_rotation(self):
        for suffix in ('.jpg','.png','.heic','.tiff'):
            with self.subTest(suffix=suffix):
                path,row=self.photo('竖图'+suffix,orientation=6)
                image,_,_=read_photo(path);expected=image.size;image.close()
                result=self.export(row);self.assertEqual(result['errors'],[])
                job=self.history.page(record_type='watermark')[0]
                with Image.open(job['output_path']) as output:
                    self.assertEqual(output.size,expected)
                    self.assertEqual(output.getexif()[274],1)

    def test_gps_preserved_and_location_caption_uses_local_record(self):
        path,row=self.photo()
        self.assertEqual(LocationService(self.catalog).run([row],39.9,116.4,location_name='北京')['complete'],1)
        row=self.history.media(row['id']);before=digest(path)
        result=self.export(row,fields=['date','location','parameters'])
        self.assertEqual(result['errors'],[])
        job=self.history.page(record_type='watermark')[0]
        self.assertIn('北京',job['rendered_text']);self.assertIn('ISO 200',job['rendered_text'])
        data=read_metadata(Path(job['output_path']),'photo')
        self.assertEqual(data['gps_status'],'present');self.assertAlmostEqual(data['latitude'],39.9)
        self.assertEqual(digest(path),before)

    def test_repeat_skips_and_watermarked_output_is_not_watermarked_again(self):
        _,row=self.photo();self.assertEqual(self.export(row)['complete'],1)
        self.assertEqual(self.export(row)['skipped'],1)
        job=self.history.page(record_type='watermark')[0]
        output=self.history.media(job['output_media_id'])
        self.assertEqual(self.export(output,style='frame')['skipped'],1)
        self.assertEqual(len(self.history.page(record_type='watermark')),1)

    def test_ready_recovery_before_and_after_publish_without_rerender(self):
        for after_publish in (False,True):
            with self.subTest(after_publish=after_publish):
                _,row=self.photo('照片'+str(after_publish)+'.jpg')
                guard=patch.object(self.service,'_finish',side_effect=OSError('database interrupted')) if after_publish else patch('watermark_service.publish_without_replace',side_effect=OSError('publication interrupted'))
                with guard:result=self.export(row)
                self.assertEqual(len(result['errors']),1)
                self.assertEqual(self.history.page(record_type='watermark')[0]['status'],'ready')
                if after_publish:self.catalog.scan(self.target)
                with patch('watermark_service.render',side_effect=AssertionError('must not rerender')):
                    result=self.export(row)
                self.assertEqual(result['errors'],[]);self.assertEqual(result['recovered'],1)
                self.assertEqual(self.history.page(record_type='watermark')[0]['status'],'complete')

    def test_collision_after_render_never_overwrites_existing_file(self):
        path,row=self.photo();original=digest(path)
        from watermark_service import publish_without_replace
        def collide(temp,output):
            Path(output).write_bytes(b'other file');publish_without_replace(temp,output)
        with patch('watermark_service.publish_without_replace',side_effect=collide):result=self.export(row)
        self.assertEqual(len(result['errors']),1)
        job=self.history.page(record_type='watermark')[0]
        self.assertEqual(Path(job['output_path']).read_bytes(),b'other file')
        self.assertEqual(digest(path),original)
        self.assertEqual(len(self.export(row)['errors']),1)

    def test_failed_metadata_validation_and_changed_source_do_not_publish(self):
        for modification in (False,True):
            with self.subTest(modification=modification):
                path,row=self.photo(str(modification)+'.jpg');fingerprint=digest(path)
                from watermark_service import copy_metadata
                def tamper(source,target,dimensions,cancel):
                    if not modification:raise ValueError('metadata mismatch')
                    copy_metadata(source,target,dimensions,cancel)
                    with Path(source).open('ab') as stream:stream.write(b'external modification')
                with patch('watermark_service.copy_metadata',side_effect=tamper):result=self.export(row)
                self.assertEqual(len(result['errors']),1)
                self.assertEqual(list(self.target.glob('*.jpg')),[])
                if not modification:self.assertEqual(digest(path),fingerprint)
                else:self.assertTrue(path.read_bytes().endswith(b'external modification'))

    def test_cancel_no_exports_and_continue_other_photos_after_failure(self):
        _,row=self.photo();cancel=threading.Event();cancel.set()
        result=self.service.run([row],self.target,DEFAULTS,cancel=cancel)
        self.assertTrue(result['stopped']);self.assertFalse(self.target.exists())
        broken,bad=self.photo('损坏照片.jpg');broken.write_bytes(b'broken')
        result=self.service.run([bad,row],self.target,DEFAULTS)
        self.assertEqual(result['complete'],1);self.assertEqual(len(result['errors']),1)
        self.assertEqual({r['status'] for r in self.history.page(record_type='watermark')},{'complete','failed'})

    def test_missing_content_is_recorded_and_long_text_does_not_overflow(self):
        path=self.source/'plain.jpg';Image.new('RGB',(600,400),'navy').save(path);self.catalog.scan(self.source)
        row=self.catalog.search()[1][0]
        result=self.service.run([row],self.target,DEFAULTS)
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(self.history.page(record_type='watermark')[0]['status'],'failed')
        with self.assertRaises(ValueError):normalize(dict(custom='x'*301))
        result=self.service.run([row],self.target,dict(custom='这是自定义文字',fields=[]))
        self.assertEqual(result['complete'],1)

    def test_logo_only_and_snapshot_is_constant_across_batch(self):
        _,first=self.photo('first.jpg');_,second=self.photo('second.jpg')
        logo=self.base/'logo.png';Image.new('RGBA',(100,40),(255,0,0,180)).save(logo)
        count=0
        def progress(_):
            nonlocal count
            count+=1
            if count==2:Image.new('RGBA',(100,40),(0,255,0,180)).save(logo)
        result=self.service.run([first,second],self.target,dict(fields=[],logo=str(logo)),progress=progress)
        self.assertEqual(result['complete'],2)
        jobs=self.history.page(record_type='watermark')
        hashes={json.loads(j['settings_json'])['logo_hash'] for j in jobs}
        self.assertEqual(len(hashes),1)

    def test_small_logo_geometry_matches_preview_and_full_resolution(self):
        logo=Image.new('RGBA',(60,20),(255,0,0,255))
        options=normalize(dict(fields=[],logo='test.png',position='top-left',opacity=100))
        bounds=[]
        for size in ((1200,800),(2400,1600)):
            image=Image.new('RGB',size,'navy');result=render(image,'',options,logo)
            bounds.append(ImageChops.difference(image,result).getbbox())
        self.assertEqual(tuple(v*2 for v in bounds[0]),bounds[1])

    def test_extended_looks_export_with_identity_metadata_and_original_preserved(self):
        from watermark_presets import look_options
        path,row=self.photo();fingerprint=digest(path)
        for style in ('gallery','postcard','film','memory','outdoor'):
            with self.subTest(style=style):
                options=dict(look_options(style),title='山海之间',signature='摄影 / 小王')
                result=self.service.run([row],self.target,options)
                self.assertEqual(result['errors'],[])
                job=self.history.page(record_type='watermark')[0]
                self.assertIn('山海之间',job['rendered_text']);self.assertIn('摄影 / 小王',job['rendered_text'])
                with Image.open(job['output_path']) as output:
                    self.assertEqual(output.getexif()[272],'Test Model')
                    self.assertEqual(output.getexif().get_ifd(34665)[36867],'2025:03:04 05:06:07')
                    if style in ('film','outdoor'):self.assertEqual(output.size,(1200,800))
                    else:self.assertGreater(output.width,1200);self.assertGreater(output.height,800)
                self.assertEqual(digest(path),fingerprint)
                self.assertEqual(self.service.run([row],self.target,options)['skipped'],1)

    def test_new_frames_preserve_every_original_pixel_in_landscape_and_portrait(self):
        from watermark_presets import look_options
        for size in ((1000,660),(660,1000)):
            original=Image.new('RGB',size,'#456789');original.paste('orange',(0,0,50,90))
            for style,proportion in (('gallery',.045),('postcard',.025),('memory',.035)):
                options=normalize(dict(look_options(style),fields=[],title='我的旅途'))
                output=render(original,'我的旅途',options)
                margin=round(min(size)*proportion)
                region=output.crop((margin,margin,margin+size[0],margin+size[1]))
                self.assertIsNone(ImageChops.difference(original,region).getbbox())
                output.close();region.close()
            original.close()

    def test_title_only_logo_only_and_empty_new_fields_keep_legacy_recipe(self):
        from watermark_presets import look_options
        _,row=self.photo()
        options=normalize(dict(fields=[],title='只有标题'))
        self.assertEqual(caption({}, {}, options),'只有标题')
        with self.assertRaises(ValueError):normalize(dict(title='标题\n下一行'))
        with self.assertRaises(ValueError):normalize(dict(signature='字'*61))
        image=Image.new('RGB',(600,900),'navy');logo=Image.new('RGBA',(80,40),(255,255,255,180))
        for style in ('gallery','postcard','film','memory','outdoor'):
            result=render(image,'',normalize(dict(look_options(style),fields=[],logo='logo.png')),logo)
            self.assertGreater(result.width,0);result.close()
        image.close();logo.close()
        self.assertEqual(self.export(row)['complete'],1)
        recipe=json.loads(self.history.page(record_type='watermark')[0]['settings_json'])
        self.assertNotIn('title',recipe);self.assertNotIn('signature',recipe)
        self.assertEqual(self.export(row)['skipped'],1)

    def test_full_import_batch_photos_and_output_import_original_lookup(self):
        _,row=self.photo();importer=ImportService(self.catalog)
        batch=importer.save_plan(importer.build_plan([row],self.source,self.base/'archive'),'copy')
        importer.run(batch)
        rows=self.service.import_photos(batch);self.assertEqual(len(rows),1)
        result=self.service.run(rows,self.target,dict(custom='测试水印'))
        self.assertEqual(result['complete'],1)
        job=self.history.page(record_type='watermark')[0]
        output=self.history.media(job['output_media_id'])
        next_batch=importer.save_plan(importer.build_plan([output],self.target,self.base/'final'),'move')
        self.assertEqual(importer.run(next_batch)['complete'],1)
        detail=self.history.details('watermark',job['id'])[0]
        self.assertIn('final',detail['current_path'])
        self.assertTrue(self.history.media(job['output_media_id'])['watermark_revision'])

    def test_v4_backup_migration_retains_original_rows(self):
        self.photo()
        with closing(self.catalog.connect()) as db,db:
            db.execute('DROP TABLE watermark_jobs');db.execute('PRAGMA user_version=4')
        self.catalog.initialize()
        self.assertEqual(self.catalog.search()[0],1)
        self.assertEqual(len(list(self.catalog.database.parent.glob('*.pre-v7-*.backup'))),1)


if __name__=='__main__':unittest.main()
