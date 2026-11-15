import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QTableView, QMessageBox

from organizer_app import MainWindow, PlanDialog, ColorDialog, STYLE, human_datetime, RowsModel
from media_catalog import read_metadata
from settings_dialog import SettingsDialog
from location_dialog import LocationDialog
from watermark_dialog import WatermarkDialog


class QtWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        from PySide6.QtCore import QRect
        screen = patch('ui_common.work_area', return_value=QRect(0, 0, 1920, 1040))
        screen.start(); self.addCleanup(screen.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'photos'
        self.source.mkdir()
        self.window = MainWindow(self.base / 'local')
        self.window.show()
        self.wait_idle()

    def wait_idle(self, timeout=20):
        deadline = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.window.busy and not self.window.task_queue.active and not self.window.jobs and not self.window.loader.jobs:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= .15:
                    return
            else:
                quiet_since = None
            time.sleep(.01)
        self.fail('UI jobs did not finish')

    def tearDown(self):
        self.wait_idle()
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_source_and_history_scan_only_selected_directory(self):
        Image.new('RGB',(30,20),'blue').save(self.source/'direct.jpg')
        child = self.source/'child'; child.mkdir()
        Image.new('RGB',(30,20),'red').save(child/'nested.jpg')
        w = self.window; w.source.setText(str(self.source))
        w.scan_source(); self.wait_idle()
        self.assertEqual([r['observed_name'] for r in w.source_model.rows], ['direct.jpg'])
        with patch('organizer_app.QFileDialog.getExistingDirectory',return_value=str(self.source)), patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Ok):
            w.index_history(); self.wait_idle()
        self.assertEqual(w.catalog.search()[0],1)

    def test_color_private_data_confirmation_tracks_save_choice(self):
        dialog = ColorDialog([], self.window, private_count=1)
        self.assertEqual(dialog.output_mode.currentData(), 'replace')
        dialog.output_mode.setCurrentIndex(0)
        dialog.confirm.setChecked(True)
        self.assertFalse(dialog.start.isEnabled())
        dialog.confirm_private.setChecked(True)
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn('原片中', dialog.private_warning.text())
        dialog.output_mode.setCurrentIndex(1)
        self.assertFalse(dialog.start.isEnabled())
        self.assertFalse(dialog.confirm_private.isChecked())
        self.assertIn('无法恢复', dialog.private_warning.text())
        dialog.confirm.setChecked(True); dialog.confirm_private.setChecked(True)
        self.assertTrue(dialog.start.isEnabled())
        dialog.reject()

    def test_no_automatic_scan_then_preview_move_and_query_original(self):
        exif = Image.Exif()
        exif[34665] = {36867: '2025:03:01 12:00:00'}
        original = self.source / 'DSC_0099.jpg'
        Image.new('RGB', (1800, 1200), 'blue').save(original, exif=exif)
        w = self.window
        self.assertEqual(w.catalog.batches(), [])
        def choose(dialog):
            dialog.add_paths([self.source]);return QDialog.DialogCode.Accepted
        with patch('organizer_app.SourceFoldersDialog.exec', choose):
            w.choose_path(w.source)
        self.assertEqual(w.catalog.batches(), [])
        w.target.setText(str(self.base / 'archive'))
        w.scan_button.click()
        self.wait_idle()
        self.assertEqual(len(w.source_model.rows), 1)
        self.assertEqual(len(w.source_table.selectionModel().selectedRows()), 1)
        self.assertTrue(w.cache.lookup(w.source_model.rows[0]))
        # First cancel the concrete preview; neither source nor target may change.
        with patch.object(PlanDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            w.preview_button.click()
            self.wait_idle()
        self.assertTrue(original.exists())
        self.assertFalse((self.base / 'archive').exists())
        with patch.object(PlanDialog, 'exec', return_value=QDialog.DialogCode.Accepted):
            w.preview_button.click()
            self.wait_idle()
        self.assertFalse(original.exists())
        self.assertEqual(w.service.history()[0]['completed_photos'], 1)
        w.search.setText('DSC_0099.jpg')
        w.refresh_library(reset=True)
        self.wait_idle()
        self.assertEqual(w.library_model.rows[0]['original_name'], 'DSC_0099.jpg')
        self.assertTrue(w.cache.lookup(w.library_model.rows[0]))

    def test_add_location_from_source_and_library_uses_single_history_table(self):
        from location_service import photo_metadata
        path = self.source/'location.jpg'; Image.new('RGB',(40,30),'blue').save(path)
        w = self.window; w.source.setText(str(self.source)); w.scan_source(); self.wait_idle()
        def pick(dialog):
            dialog.tabs.setCurrentIndex(1)
            dialog.latitude.setValue(39.9); dialog.longitude.setValue(116.4)
            self.assertFalse(dialog.start.isEnabled())
            dialog.confirm.setChecked(True)
            self.assertTrue(dialog.start.isEnabled())
            return QDialog.DialogCode.Accepted
        with patch.object(LocationDialog, 'exec', pick):
            w.source_gps_button.click(); self.wait_idle()
        self.assertEqual(photo_metadata(path)['GPS:GPSLatitude'],39.9)
        self.assertEqual([r.get('action',r['record_type']) for r in w.imports_model.rows], ['gps'])
        self.assertEqual(w.source_model.rows[0]['gps_status'],'present')
        w.tabs.setCurrentIndex(2); w.imports_table.selectRow(0); self.wait_idle()
        self.assertIn('39.9000000',w.history_details.toPlainText())
        self.assertFalse(w.retry_button.isEnabled())
        self.assertEqual(len(w.tabs.widget(2).findChildren(QTableView)),1)
        # Library now contains completed imports only, not the source scan.
        plan=w.service.build_plan(w.source_model.rows,self.source,self.base/'gps_archive')
        w.service.run(w.service.save_plan(plan,'copy'));w.refresh_library();self.wait_idle()
        w.tabs.setCurrentIndex(1); w.library_table.selectRow(0)
        with patch.object(LocationDialog, 'exec', return_value=QDialog.DialogCode.Rejected) as dialog:
            w.library_gps_button.click(); self.wait_idle()
        dialog.assert_called_once()

    def test_watermark_preview_export_and_unified_history(self):
        from import_service import digest
        path=self.source/'watermark.jpg';Image.new('RGB',(1200,800),'steelblue').save(path)
        original=digest(path);w=self.window;w.source.setText(str(self.source));w.scan_source();self.wait_idle()
        real_exec=WatermarkDialog.exec;seen=[]
        def choose(dialog):
            dialog.custom.setPlainText('中文水印 旅行记忆');dialog.target.setText(str(self.base/'watermarked'))
            deadline=time.monotonic()+10
            def finish():
                if dialog.preview_valid:
                    seen.append(dialog.pixmap.size())
                    dialog.preset_name.setText('旅行模板');dialog.save_preset();dialog.accept()
                elif time.monotonic()>deadline:
                    seen.append(dialog.preview_status.text());dialog.reject()
                else:QTimer.singleShot(25,finish)
            QTimer.singleShot(25,finish)
            return real_exec(dialog)
        with patch.object(WatermarkDialog,'exec',choose),patch.object(QMessageBox,'exec',return_value=0):
            w.source_watermark_button.click();self.wait_idle(timeout=25)
        self.assertEqual(len(seen),1)
        self.assertFalse(isinstance(seen[0],str),seen)
        self.assertEqual(digest(path),original)
        self.assertEqual([r.get('action',r['record_type']) for r in w.imports_model.rows],['watermark'])
        w.imports_table.selectRow(0);self.wait_idle()
        self.assertIn('中文水印',w.history_details.toPlainText())
        self.assertIn('另存路径',w.history_details.toPlainText())
        self.assertFalse(w.retry_button.isEnabled());self.assertFalse(w.batch_watermark_button.isEnabled())
        self.assertEqual(len(w.tabs.widget(2).findChildren(QTableView)),1)
        self.assertIn('旅行模板',str(w.settings.value('watermark_presets')))
        with patch.object(w,'show_details') as detail:
            w.browse_history_button.click();self.wait_idle()
        self.assertTrue(detail.call_args.args[0]['watermark_revision'])
        w.record_filter.setCurrentIndex(w.record_filter.findData('watermark'));self.wait_idle()
        self.assertEqual(len(w.imports_model.rows),1)

    def test_watermark_batch_entry_and_cancel_have_no_media_effects(self):
        path=self.source/'batch.jpg';Image.new('RGB',(800,500),'navy').save(path)
        w=self.window;w.source.setText(str(self.source));w.scan_source();self.wait_idle()
        plan=w.service.build_plan(w.source_model.rows,self.source,self.base/'archive')
        identifier=w.service.save_plan(plan,'copy');w.service.run(identifier)
        w.refresh_history();self.wait_idle();w.tabs.setCurrentIndex(2);w.imports_table.selectRow(0);self.wait_idle()
        self.assertTrue(w.batch_watermark_button.isEnabled())
        seen=[]
        def reject(dialog):
            seen.extend(dialog.rows);dialog.reject();return QDialog.DialogCode.Rejected
        with patch.object(WatermarkDialog,'exec',reject):
            w.batch_watermark_button.click();self.wait_idle()
        self.assertEqual(len(seen),1)
        self.assertIn('archive',seen[0]['current_path'])
        self.assertTrue(path.exists())
        self.assertEqual(w.processing_history.page(record_type='watermark'),[])

    def test_watermark_preview_reuses_decoded_photo_and_latest_settings(self):
        from watermark_service import read_photo
        Image.new('RGB',(1200,800),'navy').save(self.source/'preview.jpg')
        w=self.window;w.source.setText(str(self.source));w.scan_source();self.wait_idle()
        dialog=WatermarkDialog(w.watermark_service.enrich(w.source_model.rows),w)
        dialog.custom.setPlainText('第一版');dialog.show()
        def wait_preview():
            end=time.monotonic()+10
            while time.monotonic()<end:
                self.app.processEvents()
                if dialog.preview_valid and not dialog.preview_running:return
                time.sleep(.01)
            self.fail(dialog.preview_status.text())
        with patch('watermark_dialog.read_photo',wraps=read_photo) as decode:
            wait_preview()
            for value in range(20,90,5):dialog.opacity.setValue(value)
            dialog.custom.setPlainText('最后一版');wait_preview()
            self.assertEqual(decode.call_count,1)
            self.assertEqual(dialog.options()['custom'],'最后一版')
        dialog.reject();self.wait_idle()
        self.assertIsNone(dialog.cache)

    def test_lrf_scan_prompts_for_paired_mp4_but_never_runs_without_confirmation(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        from import_service import digest
        path = self.source/'DJI_0001.mp4'
        subprocess.run([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi',
            '-i','testsrc2=size=64x64:rate=5','-t','0.4','-c:v','libx264',str(path)],
            check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        (self.source/'DJI_0001.LRF').write_bytes(path.read_bytes())
        (self.source/'missing.LRF').write_bytes(path.read_bytes())
        before = digest(path); w=self.window; w.source.setText(str(self.source))
        seen=[]
        def prompt(box):
            seen.append(box.text())
            next(b for b in box.buttons() if b.text()=='选择还原方式').click()
            return 0
        def reject(dialog):
            self.assertEqual([r['observed_name'] for r in dialog.model.rows],['DJI_0001.mp4'])
            self.assertEqual(dialog.output_mode.currentData(),'replace')
            dialog.output_mode.setCurrentIndex(0)
            dialog.confirm.setChecked(True)
            dialog.output_mode.setCurrentIndex(1)
            self.assertFalse(dialog.confirm.isChecked())
            self.assertIn('覆盖',dialog.start.text())
            return QDialog.DialogCode.Rejected
        with patch.object(QMessageBox,'exec',prompt), patch.object(ColorDialog,'exec',reject):
            w.scan_source(); self.wait_idle()
        self.assertEqual(len(seen),1)
        self.assertIn('1 个带同名 LRF',seen[0])
        self.assertIn('处理状态未知',seen[0])
        self.assertEqual(digest(path),before)
        self.assertEqual(w.color_service.history(),[])
        w.settings.setValue('auto_color',False)
        with patch.object(QMessageBox,'exec') as prompt:
            w.scan_source(); self.wait_idle()
        prompt.assert_not_called()

    def test_real_mp4_metadata_and_thumbnail(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        path = self.source / 'CAM_001.mp4'
        subprocess.run([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=5', '-t', '1',
            '-pix_fmt', 'yuv420p', '-metadata', 'creation_time=2025-02-03T04:05:06Z', str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        w = self.window
        w.source.setText(str(self.source))
        w.scan_source()
        self.wait_idle()
        row = w.source_model.rows[0]
        self.assertEqual(row['kind'], 'video')
        self.assertAlmostEqual(row['duration_seconds'], 1, places=1)
        self.assertTrue(row['capture_time'].startswith('2025-02-03'))
        self.assertTrue(w.cache.lookup(row))

    def test_heic_metadata_and_thumbnail(self):
        from pillow_heif import register_heif_opener
        register_heif_opener()
        path = self.source / 'phone.heic'
        exif = Image.Exif()
        exif[34665] = {36867: '2025:01:02 03:04:05'}
        Image.new('RGB', (640, 480), 'red').save(path, exif=exif)
        w = self.window
        w.source.setText(str(self.source))
        w.scan_source()
        self.wait_idle()
        row = w.source_model.rows[0]
        self.assertEqual(row['gps_status'], 'missing')
        self.assertEqual(row['date_source'], 'metadata')
        self.assertTrue(w.cache.lookup(row))

    def test_color_dialog_then_restore_refresh_thumbnail_and_import(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        from import_service import digest
        path = self.source / 'DJI_0001.mp4'
        subprocess.run([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-t', '1',
            '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        w = self.window
        w.source.setText(str(self.source))
        w.target.setText(str(self.base / 'archive'))
        w.scan_source()
        self.wait_idle()
        before, old_key = digest(path), w.cache.key(w.source_model.rows[0])
        dialog = ColorDialog(w.source_model.rows, w)
        self.assertFalse(dialog.start.isEnabled())
        dialog.confirm.setChecked(True)
        self.assertTrue(dialog.start.isEnabled())
        dialog.close()
        with patch.object(ColorDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            w.color_button.click(); self.wait_idle()
        self.assertEqual(digest(path), before)
        def accept(dialog):
            dialog.output_mode.setCurrentIndex(1)
            dialog.confirm.setChecked(True)
            dialog.mode.setCurrentIndex(1)
            return QDialog.DialogCode.Accepted
        with patch.object(ColorDialog, 'exec', accept):
            w.color_button.click()
            self.wait_idle()
        self.assertEqual(w.color_service.history()[0]['status'], 'complete')
        self.assertNotEqual(digest(path), before)
        self.assertNotEqual(w.cache.key(w.source_model.rows[0]), old_key)
        self.assertTrue(w.cache.lookup(w.source_model.rows[0]))
        with patch.object(PlanDialog, 'exec', return_value=QDialog.DialogCode.Accepted):
            w.preview_button.click()
            self.wait_idle()
        self.assertFalse(path.exists())
        self.assertEqual(w.service.history()[0]['completed_videos'], 1)
        self.assertEqual(w.tabs.tabText(2), '处理记录')
        self.assertEqual(len(w.tabs.widget(2).findChildren(QTableView)), 1)
        self.assertNotIn('颜色处理记录', [b.text() for b in w.findChildren(QPushButton)])
        self.assertEqual({r.get('action',r['record_type']) for r in w.imports_model.rows}, {'color', 'import'})
        index = next(i for i, r in enumerate(w.imports_model.rows) if r.get('action',r['record_type']) == 'color')
        w.imports_table.selectRow(index)
        self.wait_idle()
        self.assertIn('DJI_0001.mp4', w.history_details.toPlainText())
        self.assertIn('编码器', w.history_details.toPlainText())
        self.assertFalse(w.retry_button.isEnabled())
        with patch.object(w, 'run_import') as run_import:
            w.retry_import()
            run_import.assert_not_called()
        with patch.object(w, 'show_details') as show_details:
            w.browse_history_button.click()
            self.wait_idle()
            self.assertEqual(show_details.call_args.args[0]['original_name'], 'DJI_0001.mp4')
        w.record_filter.setCurrentIndex(1)
        self.wait_idle()
        self.assertEqual([r['record_type'] for r in w.imports_model.rows], ['import'])
        w.record_filter.setCurrentIndex(2)
        self.wait_idle()
        self.assertEqual([r.get('action',r['record_type']) for r in w.imports_model.rows], ['color'])

    def test_selection_scope_and_primary_action_follow_actual_preview(self):
        for i in range(3):
            Image.new('RGB', (20, 20)).save(self.source / f'IMG_{i}.jpg')
        w = self.window
        self.assertEqual(w.scan_button.objectName(), 'primary')
        w.source.setText(str(self.source))
        w.target.setText(str(self.base / 'archive'))
        w.scan_source()
        self.wait_idle()
        self.assertIn('共 3 个    已选 3 个', w.source_count.text())
        self.assertEqual(w.scan_button.objectName(), '')
        self.assertEqual(w.preview_button.objectName(), 'primary')
        w.source_table.clearSelection()
        self.assertFalse(w.preview_button.isEnabled())
        self.assertFalse(w.color_button.isEnabled())
        w.source_table.selectRow(1)
        self.assertIn('已选 1 个', w.source_count.text())
        self.assertIn('（1）', w.preview_button.text())
        selected_name = w.source_model.rows[1]['observed_name']
        reviewed = []
        def check_plan(dialog):
            reviewed.extend(dialog.model.rows)
            self.assertEqual(dialog.model.headers[:3], ['原文件名', '目标文件名', '目标目录'])
            self.assertIn(dialog.model.rows[0]['target_name'], dialog.file_details.toPlainText())
            self.assertIn(dialog.model.rows[0]['target_directory'], dialog.file_details.toPlainText())
            return QDialog.DialogCode.Rejected
        with patch.object(PlanDialog, 'exec', check_plan):
            w.preview_import()
            self.wait_idle()
        self.assertEqual([r['source_name'] for r in reviewed], [selected_name])
        self.assertEqual(len(list(self.source.glob('*.jpg'))), 3)
        self.assertFalse((self.base / 'archive').exists())

    def test_naming_dialog_cancel_and_save_without_shrinking_list(self):
        w = self.window
        w.resize(1000, 680); self.app.processEvents()
        height = w.source_table.height()
        def cancel(dialog):
            dialog.template.setText('{original}')
            dialog.folder_layout.setCurrentIndex(1)
            dialog.reject()
            return QDialog.DialogCode.Rejected
        with patch.object(SettingsDialog, 'exec', cancel):
            w.settings_button.click()
        self.assertIsNone(w.settings.value('template'))
        def save(dialog):
            dialog.template.setText('{original}')
            dialog.folder_layout.setCurrentIndex(1)
            dialog.keep_named.setChecked(False)
            dialog.accept()
            return dialog.result()
        with patch.object(SettingsDialog, 'exec', save):
            w.settings_button.click(); self.wait_idle()
        self.assertEqual(w.settings.value('template'), '{original}')
        self.assertEqual(w.settings.value('folder_layout'), 'month')
        self.assertFalse(w.settings.value('keep_named', True, type=bool))
        self.assertEqual(w.source_table.height(), height)
        self.assertGreaterEqual(height, 250)

    def test_compact_list_keeps_selection_and_does_not_request_thumbnails(self):
        Image.new('RGB', (20, 20)).save(self.source / 'IMG_001.jpg')
        w = self.window
        w.source.setText(str(self.source))
        w.scan_source()
        self.wait_idle()
        w.source_compact.setChecked(True)
        self.app.processEvents()
        self.assertTrue(w.library_compact.isChecked())
        self.assertTrue(w.source_table.isColumnHidden(0))
        self.assertEqual(len(w.source_table.selectionModel().selectedRows()), 1)
        with patch.object(w.loader, 'icon', side_effect=AssertionError('compact view must not decode')):
            self.assertIsNone(w.source_model.data(w.source_model.index(0, 0), Qt.ItemDataRole.DecorationRole))
        w.library_compact.setChecked(False)
        self.assertFalse(w.source_compact.isChecked())
        self.assertFalse(w.source_table.isColumnHidden(0))
        self.assertEqual(w.source_table.horizontalHeader().stretchLastSection(), False)

    def test_display_dates_preserve_offsets_and_unknown_timezone(self):
        for stamp, expected in [
            ('2026-11-14T12:49:25+08:00', '2026-11-14 12:49:25  UTC+08:00'),
            ('2026-11-14T04:49:25Z', '2026-11-14 04:49:25  UTC+00:00'),
            ('2026-11-14T04:49:25-03:30', '2026-11-14 04:49:25  UTC-03:30'),
            ('2026-11-14 12:49:25', '2026-11-14 12:49:25'),
            ('2026-11-14', '2026-11-14')]:
            self.assertEqual(human_datetime(stamp), expected)
        model = RowsModel(['时间'], ['capture_time'])
        model.replace([{'capture_time': '2026-11-14 12:49:25'}])
        self.assertIn('时区未记录', model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole))
        self.assertEqual(model.rows[0]['capture_time'], '2026-11-14 12:49:25')


if __name__ == '__main__':
    unittest.main()
