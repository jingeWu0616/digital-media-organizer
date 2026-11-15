import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import subprocess,tempfile,time,unittest
from unittest.mock import patch
from imageio_ffmpeg import get_ffmpeg_exe
from PySide6.QtWidgets import QApplication,QMessageBox
from organizer_app import MainWindow,STYLE


class ProtectedRetryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setStyle('Fusion');cls.app.setStyleSheet(STYLE)

    def wait(self):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.01)
            if not self.w.busy and not self.w.jobs and not self.w.loader.jobs and not self.w.task_queue.active:return
        self.fail('任务未结束')

    def test_cancel_then_confirm_batch_from_existing_history(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);source=base/'素材';source.mkdir()
            first=source/'2026-11-08_Sun_DJI_20261108103752_0101_D.mp4'
            subprocess.run([get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi','-i',
                'testsrc2=size=64x64:rate=5','-t','0.4','-c:v','libx264','-movflags','+faststart',str(first)],
                capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            (source/'2026-11-08_Sun_DJI_20261108103752_0102_D.mp4').write_bytes(first.read_bytes())
            w=self.w=MainWindow(base/'data');w.settings.setValue('auto_color',False);w.set_source_paths([source]);w.show();w.scan_source();self.wait()
            from import_service import digest
            original={r['current_path']:digest(r['current_path']) for r in w.source_model.rows}
            w.enqueue('gps',w.source_model.rows,dict(latitude=30.,longitude=120.,write_mode='fast',overwrite=False));self.wait()
            w.tabs.setCurrentIndex(2);w.refresh_history();self.wait();w.imports_table.selectRow(0);self.wait()
            self.assertEqual(w.retry_button.text(),'改用保护写入');self.assertTrue(w.retry_button.isEnabled())
            def cancel(box):
                box.show()
                for _ in range(10):self.app.processEvents()
                self.assertIn('不重新编码',box.text());self.assertIn('30.0000000',box.text())
                self.assertIn(str(source),box.detailedText())
                if os.environ.get('CAPTURE_RETRY_UI'):
                    folder=Path('work/followup_415');folder.mkdir(parents=True,exist_ok=True)
                    box.grab().save(str(folder/('protected-confirm-'+os.environ.get('QT_SCALE_FACTOR','1')+'.png')))
                next(b for b in box.buttons() if b.text()=='取消').click()
                return 0
            with patch.object(QMessageBox,'exec',cancel):w.retry_button.click();self.wait()
            self.assertEqual(len(w.task_queue.snapshot()),2)
            self.assertEqual({p:digest(p) for p in original},original)
            def confirm(box):
                next(b for b in box.buttons() if b.text()=='本批跳过的 2 个视频').click();return 0
            with patch.object(QMessageBox,'exec',confirm):w.retry_button.click();self.wait()
            self.assertEqual(len(w.task_queue.snapshot()),4)
            self.assertTrue(all(r['gps_status']=='present' for r in w.catalog.search()[1]))
            self.assertEqual(len(w.processing_history.page(record_type='gps')),4)
            w.close();self.app.processEvents()


if __name__=='__main__':unittest.main()
