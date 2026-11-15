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

from color_service import ColorService, LUT_PATH, LUT_HASH, profile, command, lrf_candidates
from import_service import ImportService, digest, Cancelled
from media_catalog import Catalog
from thumbnail_cache import ThumbnailCache


class ColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.clips = {}
        for codec, pixel, rate in [('libx264', 'yuv420p', '25'), ('libx265', 'yuv420p10le', '30000/1001')]:
            target = Path(cls.fixtures.name) / (codec + '.mp4')
            subprocess.run([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=' + rate,
                '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', codec,
                '-pix_fmt', pixel, '-c:a', 'aac', '-metadata', 'creation_time=2025-02-03T04:05:06Z',
                str(target)], check=True, capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            cls.clips[codec] = target

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / '素材'
        self.source.mkdir()
        self.catalog = Catalog(self.base / 'local/catalog.sqlite3')
        self.catalog.initialize()
        self.service = ColorService(self.catalog)

    def tearDown(self):
        self.temp.cleanup()

    def clip(self, codec='libx264', name='DJI_0001.mp4'):
        target = self.source / name
        shutil.copy2(self.clips[codec], target)
        self.scan()
        return target

    def scan(self):
        self.catalog.scan(self.source, batch_kind='source_scan')
        return self.catalog.search(limit=10000)[1]

    def run_color(self, **kwargs):
        return self.service.run(self.catalog.search()[1], self.source,
                                confirmed_dlogm=True, mode='cpu', **kwargs)

    def test_lut_matches_official_asset_and_confirmation_required(self):
        self.assertEqual(digest(LUT_PATH), LUT_HASH)
        self.clip()
        with self.assertRaises(ValueError):
            self.service.run(self.catalog.search()[1], self.source)
        self.assertEqual(self.service.history(), [])

    def test_lrf_candidates_same_directory_case_and_missing_pairs(self):
        def row(name, identifier, revision=''):
            return dict(current_path=str(self.source/name), id=identifier, color_revision=revision)
        rows = [row('DJI_0001.mp4','1'), row('DJI_0001.LRF','2'),
            row('other/DJI_0001.lrf','3'), row('unpaired.lrf','4'),
            row('done.mp4','5','hash'), row('done.lrf','6'), row('plain.mp4','7')]
        candidates, missing = lrf_candidates(rows)
        self.assertEqual([r['id'] for r in candidates], ['1'])
        self.assertEqual(len(missing), 2)

    def test_copy_mode_preserves_original_and_indexes_output_with_original_name(self):
        source = self.clip(); before = digest(source)
        occupied = source.with_name(source.stem + '_Rec709.mp4'); occupied.write_bytes(b'occupied')
        rows = [r for r in self.catalog.search()[1] if r['observed_name'] == source.name]
        result = self.service.run(rows, self.source, confirmed_dlogm=True, mode='cpu', output_mode='copy')
        self.assertEqual(result['errors'], [])
        self.assertEqual(digest(source), before)
        job = self.service.history()[0]; output = Path(job['output_path'])
        self.assertEqual(output.name, source.stem + '_Rec709_2.mp4')
        self.assertEqual(occupied.read_bytes(), b'occupied')
        self.assertNotEqual(digest(output), before)
        indexed = next(r for r in self.catalog.search()[1] if r['id'] == job['output_media_id'])
        self.assertEqual(indexed['original_name'], source.name)
        self.assertEqual(indexed['color_revision'], digest(output))
        original = next(r for r in self.catalog.search()[1] if r['observed_name'] == source.name)
        proxy = dict(original, id='proxy', current_path=str(source.with_suffix('.LRF')))
        self.assertEqual(self.service.auto_candidates([original, proxy])[0], [])
        self.assertEqual(self.service.run(rows, self.source, confirmed_dlogm=True, output_mode='copy')['skipped'], 1)

    def nas_clip(self, name):
        if os.name != 'nt' or not self.source.drive:
            self.skipTest('Windows SMB loopback share required')
        root = Path('\\\\localhost\\' + self.source.drive[0] + '$' + str(self.source)[2:])
        if not root.is_dir():
            self.skipTest('Local administrative SMB share is not available; no share is created')
        local = self.source / name
        shutil.copy2(self.clips['libx264'], local)
        self.catalog.scan(root, recursive=False, batch_kind='source_scan')
        row = next(r for r in self.catalog.search()[1] if r['observed_name'] == name)
        self.assertTrue(row['current_path'].startswith('\\\\'))
        return root, local, row

    def test_smb_source_copy_and_replace_with_local_catalog(self):
        for mode in ('copy', 'replace'):
            with self.subTest(mode=mode):
                root, local, row = self.nas_clip('NAS_' + mode + '.mp4')
                original = digest(local)
                # Exercise the same automatic LRF reminder with a network root.
                proxy = local.with_suffix('.LRF'); shutil.copy2(local, proxy)
                self.catalog.scan(root, recursive=False, batch_kind='source_scan')
                candidates, missing = self.service.auto_candidates(self.catalog.search()[1])
                self.assertIn(row['id'], [r['id'] for r in candidates])
                result = self.service.run([row], root, confirmed_dlogm=True, mode='cpu', output_mode=mode)
                self.assertEqual(result['errors'], [], result)
                self.assertEqual(result['complete'], 1)
                job = self.service.history()[0]
                self.assertEqual(Path(job['temp_path']).parent, root.resolve())
                self.assertEqual(digest(Path(job['output_path'])), job['output_hash'])
                self.assertEqual(digest(proxy), original)
                self.assertEqual(digest(local) == original, mode == 'copy')
                self.assertFalse(str(self.catalog.database).startswith('\\\\'))

    def test_smb_publication_interruption_recovers_without_reencoding(self):
        for published in (False, True):
            for mode in ('copy', 'replace'):
                with self.subTest(published=published, mode=mode):
                    self.catalog = Catalog(self.base / f'data-{published}-{mode}/catalog.sqlite3')
                    self.catalog.initialize(); self.service = ColorService(self.catalog)
                    root, local, row = self.nas_clip(f'interrupted_{published}_{mode}.mp4')
                    before = digest(local)
                    publish = self.service._publish
                    def interrupted(*args):
                        if published:
                            publish(*args)
                        raise ConnectionError('simulated lost SMB acknowledgement')
                    with patch.object(self.service, '_publish', side_effect=interrupted):
                        result = self.service.run([row], root, confirmed_dlogm=True, mode='cpu', output_mode=mode)
                    self.assertEqual(len(result['errors']), 1, result)
                    job = self.service.history()[0]
                    self.assertEqual(job['status'], 'ready')
                    if not published or mode == 'copy':
                        self.assertEqual(digest(local), before)
                    with patch('color_service.command', side_effect=AssertionError('must not reencode')):
                        result = self.service.run([row], root, confirmed_dlogm=True, mode='cpu', output_mode=mode)
                    self.assertEqual(result['complete'], 1, result)
                    self.assertEqual(result['errors'], [])
                    self.assertEqual(digest(Path(job['output_path'])), job['output_hash'])
                    self.assertEqual(self.service.history()[0]['status'], 'complete')

    def test_smb_encoding_failure_keeps_source_and_records_failure(self):
        root, local, row = self.nas_clip('encoding_failure.mp4')
        before = digest(local)
        def disconnected(args, *a, **kw):
            Path(args[-1]).write_bytes(b'unvalidated partial output')
            raise ConnectionError('simulated SMB read interrupted')
        with patch('color_service.command', side_effect=disconnected):
            result = self.service.run([row], root, confirmed_dlogm=True, mode='cpu')
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(digest(local), before)
        self.assertEqual(self.service.history()[0]['status'], 'failed')
        self.assertEqual(list(root.glob('.mediaorganizer-color-*.part')), [])

    def test_network_mapped_database_still_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows drive types')
        with patch('media_catalog.ctypes.windll.kernel32.GetDriveTypeW', return_value=4):
            with self.assertRaisesRegex(ValueError, '网络映射盘'):
                Catalog(self.base / 'mapped-drive-catalog.sqlite3')

    def test_multi_roots_and_real_encoder_progress(self):
        first = self.clip()
        other = self.base / 'second'; other.mkdir()
        shutil.copy2(self.clips['libx265'], other / 'second.mp4')
        batch = self.catalog.scan([self.source, other], recursive=False)
        rows = self.catalog.search(batch_id=batch['batch_id'])[1]
        events = []
        result = self.service.run(rows + rows[:1], [self.source, other], confirmed_dlogm=True,
                                  mode='cpu', progress=events.append)
        self.assertEqual(result['complete'], 2, result)
        self.assertTrue(all(e['total'] == 2 for e in events))
        self.assertEqual(events[-1]['processed'], 2)
        encoding = [e for e in events if e['phase']=='还原颜色']
        self.assertTrue(any(e['percent'] > 0 for e in encoding))
        self.assertTrue(all(0 <= e['percent'] <= 100 for e in encoding))
        self.assertTrue(any(e['phase']=='校验视频' and e['percent'] is None for e in events))
        self.assertTrue(any(e['phase']=='保存成品' for e in events))

    def test_gps_edit_keeps_color_identity_and_pending_gps_blocks_color(self):
        from location_service import LocationService
        source=self.clip();result=self.run_color();self.assertEqual(result['complete'],1,result)
        location=LocationService(self.catalog);row=self.catalog.search()[1][0]
        with patch.object(location,'_finish',side_effect=OSError('interrupted GPS journal')):
            self.assertEqual(len(location.run([row],39.9,116.4)['errors']),1)
        result=self.run_color();self.assertEqual(result['complete'],0);self.assertTrue(result['errors'])
        self.assertEqual(location.run([row],39.9,116.4)['recovered'],1)
        before=digest(source)
        with patch('color_service.command',side_effect=AssertionError('GPS must not cause another LUT')):
            result=self.run_color()
        self.assertEqual(result['skipped'],1,result);self.assertEqual(digest(source),before)

    def test_color_restoration_preserves_previously_written_precise_gps(self):
        from location_service import LocationService
        from media_catalog import read_metadata
        source=self.clip();location=LocationService(self.catalog)
        self.assertEqual(location.run(self.catalog.search()[1],39.1234567,117.7654321)['complete'],1)
        result=self.run_color();self.assertEqual(result['complete'],1,result)
        values=read_metadata(source,'video')
        self.assertAlmostEqual(values['latitude'],39.1234567,places=7)
        self.assertAlmostEqual(values['longitude'],117.7654321,places=7)

    def test_copy_recovery_does_not_reencode_or_replace_source(self):
        source = self.clip(); before = digest(source)
        with patch.object(self.service, '_finish', side_effect=RuntimeError('DB interrupted')):
            result = self.run_color(output_mode='copy')
        self.assertEqual(len(result['errors']), 1)
        job = self.service.history()[0]
        self.assertEqual(job['status'], 'ready')
        self.assertTrue(Path(job['output_path']).is_file())
        # A rescan can index the new file under a new ID before journal recovery.
        self.scan()
        row = next(r for r in self.catalog.search()[1] if r['observed_name'] == source.name)
        with patch('color_service.command', side_effect=AssertionError('must not encode')):
            result = self.service.run([row], self.source, confirmed_dlogm=True, output_mode='copy')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['complete'], 1)
        self.assertEqual(digest(source), before)
        self.assertEqual(self.service.history()[0]['status'], 'complete')

    def test_copy_publish_conflict_is_preserved_and_mode_change_does_not_overwrite(self):
        source = self.clip(); before = digest(source)
        publish = self.service._publish
        def conflict(temp, output, mode):
            output.write_bytes(b'other file')
            publish(temp, output, mode)
        with patch.object(self.service, '_publish', side_effect=conflict):
            result = self.run_color(output_mode='copy')
        self.assertEqual(len(result['errors']), 1)
        job = self.service.history()[0]
        self.assertEqual(Path(job['output_path']).read_bytes(), b'other file')
        self.assertEqual(len(self.run_color(output_mode='replace')['errors']), 1)
        self.assertEqual(digest(source), before)

    def test_codec_dimensions_frames_depth_audio_date_and_original_query_after_move(self):
        for codec in ('libx264', 'libx265'):
            with self.subTest(codec=codec):
                source = self.clip(codec, codec + '.mp4')
                before, fingerprint, stamp = profile(source), digest(source), source.stat().st_mtime_ns
                rows = [r for r in self.catalog.search()[1] if r['observed_name'] == source.name]
                cache = ThumbnailCache(self.base / 'thumbs')
                old_key = cache.key(rows[0])
                result = self.service.run(rows, self.source, confirmed_dlogm=True, mode='cpu')
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['complete'], 1)
                after = profile(source)
                for field in ('format', 'width', 'height', 'frame_count', 'frame_rate', 'bit_depth', 'capture'):
                    self.assertEqual(before[field], after[field], field)
                self.assertNotEqual(fingerprint, digest(source))
                self.assertEqual(stamp, source.stat().st_mtime_ns)
                row = self.catalog.search(source.name)[1][0]
                self.assertEqual(row['content_hash'], digest(source))
                self.assertNotEqual(old_key, cache.key(row))
                transfer = ImportService(self.catalog, cache)
                plan = transfer.build_plan([row], self.source, self.base / 'archive')
                batch = transfer.save_plan(plan)
                self.assertEqual(transfer.run(batch)['complete'], 1)
                moved = self.catalog.search(source.name)[1][0]
                self.assertEqual(moved['original_name'], source.name)
                self.assertTrue(moved['color_revision'])
                self.assertEqual(self.service.history()[0]['current_path'], moved['current_path'])

    def test_lrf_untouched_and_repeat_after_copy_or_rename_skips(self):
        source = self.clip()
        proxy = self.clip(name='DJI_0001.LRF')
        before = digest(proxy)
        first = self.run_color()
        self.assertEqual((first['complete'], first['skipped']), (1, 1))
        self.assertEqual(digest(proxy), before)
        restored_hash = digest(source)
        renamed = self.source / 'renamed.mp4'
        source.rename(renamed)
        shutil.copy2(renamed, self.source / 'copy.mp4')
        self.scan()
        rows = [r for r in self.catalog.search()[1] if Path(r['current_path']).exists()]
        result = self.service.run(rows, self.source, confirmed_dlogm=True, mode='cpu')
        self.assertEqual(result['complete'], 0)
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(digest(renamed), restored_hash)
        self.assertEqual(len(self.service.history()), 1)

    def test_validation_failure_never_overwrites(self):
        source = self.clip()
        before = digest(source)
        with patch.object(self.service, '_validate', side_effect=ValueError('injected validation failure')):
            result = self.run_color()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(digest(source), before)
        self.assertEqual(self.service.history()[0]['status'], 'failed')
        self.assertEqual(list(self.source.glob('*.part')), [])

    def test_cover_art_is_copied_without_applying_main_video_filter(self):
        cover = self.base / 'cover.jpg'
        Image.new('RGB', (80, 60), 'red').save(cover)
        source = self.source / 'covered.mp4'
        command(['-i', str(self.clips['libx264']), '-i', str(cover),
            '-map', '0', '-map', '1:v', '-c', 'copy', '-disposition:v:1', 'attached_pic',
            '-metadata:s:v:0', 'timecode=01:02:03:04',
            str(source)], threading.Event())
        self.scan()
        result = self.run_color(output_mode='copy')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['complete'], 1)

    def test_private_data_loss_requires_explicit_consent_before_encoding(self):
        source = self.clip()
        before = digest(source)
        spec = profile(source)
        spec['aux'].append(dict(codec_id='djmd', streamorder='2'))
        with patch('color_service.profile', return_value=spec), patch.object(self.service, '_encoder') as encoder:
            result = self.run_color()
        encoder.assert_not_called()
        self.assertIn('确认允许', result['errors'][0])
        self.assertEqual(digest(source), before)

    def test_cancel_in_encoder_retains_original_and_cleans_owned_temp(self):
        source = self.clip()
        before = digest(source)
        cancel = threading.Event()
        def interrupted(args, *a, **kw):
            Path(args[-1]).write_bytes(b'partial')
            cancel.set()
            raise Cancelled()
        with patch('color_service.command', side_effect=interrupted):
            result = self.run_color(cancel=cancel)
        self.assertTrue(result['stopped'])
        self.assertEqual(digest(source), before)
        self.assertEqual(list(self.source.glob('*.part')), [])

    def test_external_modification_during_processing_is_not_overwritten(self):
        source = self.clip()
        validate = self.service._validate
        def changed(*args):
            validate(*args)
            with source.open('ab') as stream:
                stream.write(b'external modification')
        with patch.object(self.service, '_validate', side_effect=changed):
            result = self.run_color()
        self.assertEqual(len(result['errors']), 1)
        self.assertTrue(source.read_bytes().endswith(b'external modification'))

    def test_recovery_after_replace_before_database_commit_no_double_lut(self):
        source = self.clip()
        with patch.object(self.service, '_finish', side_effect=RuntimeError('simulated DB failure')):
            result = self.run_color()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(self.service.history()[0]['status'], 'ready')
        output = digest(source)
        self.scan()
        with self.assertRaises(ValueError):
            ImportService(self.catalog).build_plan(self.catalog.search()[1], self.source, self.base / 'archive')
        with patch('color_service.command', side_effect=AssertionError('must not reencode')):
            result = self.run_color()
        self.assertEqual(result['complete'], 1)
        self.assertEqual(digest(source), output)
        self.assertEqual(self.service.history()[0]['status'], 'complete')
        self.assertEqual(self.catalog.search()[1][0]['color_revision'], output)

    def test_recovery_before_replace_uses_verified_output(self):
        source = self.clip()
        before = digest(source)
        with patch('color_service.os.replace', side_effect=PermissionError('file in use')):
            self.run_color()
        self.assertEqual(digest(source), before)
        job = self.service.history()[0]
        self.assertEqual(job['status'], 'ready')
        self.assertTrue(Path(job['temp_path']).exists())
        with patch('color_service.command', side_effect=AssertionError('must not reencode')):
            self.assertEqual(self.run_color()['complete'], 1)
        self.assertEqual(digest(source), job['output_hash'])

    def test_pending_import_prevents_source_replacement(self):
        source = self.clip()
        transfer = ImportService(self.catalog)
        plan = transfer.build_plan(self.catalog.search()[1], self.source, self.base / 'archive')
        transfer.save_plan(plan)
        before = digest(source)
        result = self.run_color()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(before, digest(source))

    def test_existing_v2_database_is_backed_up_before_migration(self):
        self.clip()
        with closing(self.catalog.connect()) as db, db:
            db.execute('DROP TABLE color_jobs')
            db.execute('ALTER TABLE media DROP COLUMN color_revision')
            db.execute('PRAGMA user_version=2')
        self.catalog.initialize()
        self.assertEqual(self.catalog.search()[0], 1)
        backup = list(self.catalog.database.parent.glob('*.pre-v7-*.backup'))
        self.assertEqual(len(backup), 1)
        with closing(sqlite3.connect(backup[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_changed_file_clears_cached_color_fingerprint(self):
        source = self.clip()
        self.assertEqual(self.run_color()['complete'], 1)
        source.write_bytes(self.clips['libx264'].read_bytes())
        rows = self.scan()
        self.assertFalse(rows[0]['color_revision'])
        self.assertIsNone(rows[0]['content_hash'])

    def test_actual_hardware_failure_retries_cpu_without_touching_source_early(self):
        source = self.clip()
        before = digest(source)
        real_encoder = self.service._encoder
        def encoder(spec, mode, cancel):
            if mode == 'auto':
                return ('h264_qsv', ['-global_quality', '18'], 'nv12')
            return real_encoder(spec, mode, cancel)
        real_command = command
        def fake_hardware(args, *a, **kw):
            if 'h264_qsv' in args:
                self.assertEqual(digest(source), before)
                Path(args[-1]).write_bytes(b'unfinished hardware output')
                raise RuntimeError('hardware cannot encode this resolution')
            return real_command(args, *a, **kw)
        with patch.object(self.service, '_encoder', side_effect=encoder), patch('color_service.command', side_effect=fake_hardware):
            result = self.service.run(self.catalog.search()[1], self.source, confirmed_dlogm=True)
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.service.history()[0]['encoder'], 'libx264')

    def test_missing_frames_are_rejected(self):
        source = self.clip()
        spec = profile(source)
        spec['frame_count'] = '999'
        with self.assertRaisesRegex(ValueError, 'frame_count'):
            self.service._validate(source, source, spec, threading.Event(), lambda: None)


if __name__ == '__main__':
    unittest.main()
