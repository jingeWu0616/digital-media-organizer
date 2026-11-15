import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,time,unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import QTimer,QRect,QPoint
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox
from organizer_app import MainWindow,STYLE
from activity_dialog import ActivityDialog
from location_dialog import LocationDialog


class ActivityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setStyle('Fusion');cls.app.setStyleSheet(STYLE)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.source=self.base/'source';self.source.mkdir()
        for i in range(2):Image.new('RGB',(120,80),(40*i,30,10)).save(self.source/f'DSC_{i}.jpg')
        self.screen=patch('ui_common.work_area',return_value=QRect(0,0,1920,1040));self.screen.start()
        self.w=MainWindow(self.base/'data');self.w.show();self.wait();self.w.source.setText(str(self.source));self.w.scan_source();self.wait()
    def wait(self,predicate=None):
        end=time.monotonic()+15;quiet=None
        while time.monotonic()<end:
            self.app.processEvents();time.sleep(.01)
            if not self.w.jobs and not self.w.loader.jobs and not self.w.busy and not self.w.task_queue.active and (predicate is None or predicate()):
                quiet=quiet or time.monotonic()
                if time.monotonic()-quiet>.2:return
            else:quiet=None
        self.fail('UI did not finish')
    def tearDown(self):
        self.wait();self.w.close();self.app.processEvents();self.screen.stop();self.temp.cleanup()
    def test_main_entry_save_import_library_activity_and_history(self):
        w=self.w;w.source_table.clearSelection();w.source_table.selectRow(0)
        real_exec=ActivityDialog.exec
        def edit(d):
            def fill():
                d.activity.setCurrentIndex(d.activity.findData('new'));d.name.setText('海边亲子游');d.companions.setText('家人')
                d.toggles['place'].setChecked(True);d.fields['place'].setText('东疆湾');d.save.click()
            QTimer.singleShot(10,fill);return real_exec(d)
        with patch.object(ActivityDialog,'exec',edit):w.source_activity_button.click();self.wait()
        rows=w.catalog.search()[1];self.assertEqual(sum(bool(r['activity_id']) for r in rows),1)
        w.refresh_library();self.wait();self.assertEqual(w.library_total,0)
        plan=w.service.build_plan(rows,self.source,self.base/'archive');w.service.run(w.service.save_plan(plan,'copy'))
        w.search.setText('家人');w.refresh_library();self.wait();self.assertEqual(w.library_total,1)
        row=w.library_model.rows[0];w.show_activity(row['activity_id'],row['activity_name']);self.wait()
        self.assertEqual(w.library_total,1);self.assertTrue(w.activity_scope.isVisible())
        w.clear_import_filter();self.wait();self.assertEqual(w.library_total,2)
        w.record_filter.setCurrentIndex(w.record_filter.findData('activity'));w.refresh_history();self.wait()
        self.assertEqual(len(w.imports_model.rows),1);self.assertIn('东疆湾',w.history_details.toPlainText())
        self.assertFalse(w.retry_button.isEnabled());self.assertFalse(w.browse_history_button.isEnabled())
    def test_failure_keeps_input_mixed_values_and_clear_confirmation(self):
        w=self.w;rows=w.activity_service.rows([r['id'] for r in w.source_model.rows])
        w.activity_service.apply(rows[:1],patch=dict(place='海边'));rows=w.activity_service.rows([r['id'] for r in rows])
        d=ActivityDialog(rows,[],w);d.show();self.wait()
        self.assertIn('多个值',d.fields['place'].placeholderText());self.assertFalse(d.toggles['place'].isChecked())
        d.clear_field('place')
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):d.accept()
        self.assertTrue(d.isVisible());self.assertEqual(w.catalog.search(text='海边')[0],1)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Ok),patch.object(w.activity_service,'apply',side_effect=ValueError('模拟保存失败')):
            d.accept();self.wait()
        self.assertTrue(d.isVisible());self.assertTrue(d.toggles['place'].isChecked());self.assertIn('模拟保存失败',d.message.text())
        d.reject();self.wait();self.assertEqual(w.catalog.search(text='海边')[0],1)
    def test_constrained_dialog_and_video_confirmation(self):
        w=self.w;area=QRect(0,0,960,520)
        with patch('ui_common.work_area',return_value=area):
            d=ActivityDialog(w.source_model.rows,[],w);d.show();self.wait()
            self.assertTrue(area.contains(d.geometry()));self.assertTrue(d.rect().contains(QRect(d.save.mapTo(d,QPoint(0,0)),d.save.size())))
            self.assertEqual(d.content_scroll.horizontalScrollBar().maximum(),0);d.reject()
            loc=LocationDialog(2,w,video_count=1);loc.show();self.wait();loc.tabs.setCurrentIndex(1)
            self.app.processEvents()
            viewport=loc.content_scroll.viewport()
            self.assertTrue(viewport.rect().contains(QRect(loc.confirm.mapTo(viewport,QPoint(0,0)),loc.confirm.size())))
            loc.latitude.setValue(30);loc.longitude.setValue(120);self.assertFalse(loc.start.isEnabled())
            loc.confirm.setChecked(True);self.assertTrue(loc.start.isEnabled());loc.overwrite.setChecked(True);self.assertFalse(loc.start.isEnabled());loc.reject()

    def test_fast_mode_clears_confirmation_and_hides_for_photos_and_place_picker(self):
        loc=LocationDialog(2,self.w,video_count=1);loc.show();self.app.processEvents()
        self.assertEqual(loc.write_mode.currentData(),'protected')
        loc.tabs.setCurrentIndex(1);loc.latitude.setValue(30);loc.longitude.setValue(120)
        loc.confirm.setChecked(True);self.assertTrue(loc.start.isEnabled())
        loc.write_mode.setCurrentIndex(loc.write_mode.findData('fast'))
        self.assertFalse(loc.confirm.isChecked());self.assertFalse(loc.start.isEnabled())
        self.assertIn('损坏',loc.confirm.text())
        loc.confirm.setChecked(True);loc.write_mode.setCurrentIndex(0)
        self.assertFalse(loc.confirm.isChecked());loc.reject()
        for options in ({}, {'video_count':1,'picker_only':True}):
            loc=LocationDialog(1,self.w,**options);loc.show();self.app.processEvents()
            self.assertFalse(loc.write_mode.isVisible());loc.reject()

    def test_selected_video_gps_uses_real_entry_and_unified_history(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        from import_service import digest
        w=self.w;video=self.source/'DJI_0003.mp4'
        subprocess.run([get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25','-t','0.2','-c:v','libx264',str(video)],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        before={str(p):digest(p) for p in self.source.glob('*.jpg')}
        w.scan_source();self.wait();w.source_table.clearSelection()
        index=next(i for i,r in enumerate(w.source_model.rows) if r['kind']=='video');w.source_table.selectRow(index)
        self.assertTrue(w.source_gps_button.isEnabled())
        real_exec=LocationDialog.exec
        def choose(d):
            def fill():
                d.tabs.setCurrentIndex(1);d.latitude.setValue(39.1);d.longitude.setValue(117.7)
                d.write_mode.setCurrentIndex(d.write_mode.findData('fast'));d.confirm.setChecked(True);d.start.click()
            QTimer.singleShot(10,fill);return real_exec(d)
        with patch.object(LocationDialog,'exec',choose),patch.object(QMessageBox,'exec',return_value=0):w.source_gps_button.click();self.wait()
        row=w.catalog.search(kind='video')[1][0];self.assertEqual(row['gps_status'],'present');self.assertAlmostEqual(row['latitude'],39.1)
        self.assertEqual({p:digest(p) for p in before},before)
        records=w.processing_history.page(record_type='gps');self.assertEqual(len(records),1);self.assertEqual(records[0]['completed_videos'],1)
        self.assertEqual(records[0]['type_label'],'写入定位 · 快速')


if __name__=='__main__':unittest.main()
