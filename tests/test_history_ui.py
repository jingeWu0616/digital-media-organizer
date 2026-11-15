import tempfile
import functools
import os
import subprocess
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from history_index import HistoryIndexWindow
from media_catalog import Catalog


def isolated_tk(function):
    """Keep legacy Tcl lifetime separate from Qt's worker-thread GC in discovery."""
    @functools.wraps(function)
    def run(self):
        if os.environ.get('MEDIA_TK_TEST_CHILD') == '1':
            return function(self)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
            '-p', 'test_history_ui.py', '-k', function.__name__, '-v'],
            env=dict(os.environ, MEDIA_TK_TEST_CHILD='1'), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return run


class HistoryUITests(unittest.TestCase):
    @isolated_tk
    def test_manual_selection_scan_and_search(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'photos'
            source.mkdir()
            Image.new('RGB', (20, 20)).save(source / '2024-01-01_Mon_Cam_IMG_0012.jpg')
            root = tk.Tk()
            root.withdraw()
            window = HistoryIndexWindow(root, base / 'catalog.sqlite3')
            window.withdraw()
            try:
                self.assertEqual(window.catalog.batches(), [])
                with patch('history_index.filedialog.askdirectory', return_value=str(source)):
                    window.choose_button.invoke()
                self.assertEqual(window.catalog.batches(), [])
                self.assertFalse(window.scanning)
                window.scan_button.invoke()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    root.update()
                    if not window.scanning and window.table.get_children():
                        break
                    time.sleep(0.01)
                self.assertFalse(window.scanning)
                self.assertEqual(len(window.table.get_children()), 1)
                window.query.set('IMG_0012')
                window.refresh(reset=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    root.update()
                    if window.total == 1 and window.table.get_children():
                        break
                    time.sleep(0.01)
                self.assertEqual(window.total, 1)
                self.assertEqual(len(window.catalog.batches()), 1)
            finally:
                window.close()
                root.destroy()

    @isolated_tk
    def test_main_application_entry_opens_without_scanning(self):
        import organize_media
        with tempfile.TemporaryDirectory() as folder:
            catalog = Catalog(Path(folder) / 'catalog.sqlite3')
            app = organize_media.ModernMediaOrganizer()
            app.withdraw()
            try:
                with patch('history_index.Catalog', return_value=catalog):
                    app.history_btn.invoke()
                app.history_window.withdraw()
                app.update()
                self.assertEqual(catalog.batches(), [])
                self.assertFalse(app.history_window.scanning)
                first = app.history_window
                app.history_btn.invoke()
                self.assertIs(app.history_window, first)
            finally:
                if app.history_window:
                    app.history_window.close()
                app.update()
                # CustomTkinter schedules global timers. Multiple Tk roots in
                # one test process require clearing them before destroying one.
                for timer in app.tk.call('after', 'info'):
                    app.tk.call('after', 'cancel', timer)
                app.destroy()


if __name__ == '__main__':
    unittest.main()
