import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from tests import test_task_ui as fixture
from video_gps import GPS_COMPATIBILITY_REQUIRED


class ColorGpsUiTests(unittest.TestCase):
    setUpClass=classmethod(fixture.TaskUiTests.setUpClass.__func__)
    setUp=fixture.TaskUiTests.setUp
    tearDown=fixture.TaskUiTests.tearDown
    wait=fixture.TaskUiTests.wait

    def test_compatible_action_requires_confirmation_and_keeps_default_cancel(self):
        w=self.w;row=w.source_model.rows[0]
        with patch.object(w.task_queue,'runner',return_value=dict(complete=0,skipped=0,errors=[GPS_COMPATIBILITY_REQUIRED])):
            w.task_queue.submit('color',[row],dict(source_root=str(self.source),confirmed_dlogm=True,mode='cpu'))
            self.wait()
        w.tabs.setCurrentIndex(2);w.refresh_history();self.wait()
        self.assertEqual(w.retry_button.text(),'兼容保留定位')
        record=w.selected_import();seen=[]
        for action in ('取消','兼容处理'):
            def choose(action=action):
                dialog=next((d for d in self.app.topLevelWidgets() if isinstance(d,QMessageBox) and d.isVisible()),None)
                if dialog is None:QTimer.singleShot(10,choose);return
                seen.append(dialog.defaultButton().text());self.assertIn('整文件读写',dialog.text())
                folder=Path(__file__).resolve().parents[1]/'work/gps_color_417';folder.mkdir(parents=True,exist_ok=True)
                dialog.grab().save(str(folder/f'compatible-dialog-{self.app.primaryScreen().devicePixelRatio():g}.png'))
                next(b for b in dialog.buttons() if b.text()==action).click()
            with patch.object(w.task_queue,'retry') as retry:
                QTimer.singleShot(10,choose);w.retry_import()
                if action=='取消':retry.assert_not_called()
                else:retry.assert_called_once_with(record['id'],compatible_gps=True)
        self.assertEqual(seen,['取消','取消'])


if __name__=='__main__':unittest.main()
