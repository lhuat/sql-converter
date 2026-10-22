"""GUI smoke tests. The Windows packaging job runs these with Qt offscreen."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication, QFrame, QWidget
    from sql_converter.gui import Window
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False


@unittest.skipUnless(GUI_AVAILABLE, 'PySide6 is not installed on this Python host')
class GuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sections_have_independent_headers(self):
        win = Window()
        win.show()
        self.app.processEvents()
        try:
            cards = win.findChildren(QFrame, 'sectionCard')
            self.assertEqual(len(cards), 3)
            for card in cards:
                header = card.findChild(QWidget, 'sectionHeader')
                body = card.findChild(QWidget, 'sectionBody')
                self.assertIsNotNone(header)
                self.assertIsNotNone(body)
                self.assertGreater(header.height(), 0)
                self.assertGreaterEqual(body.y(), header.y() + header.height())
            self.assertEqual(win.report_text.height(), 116)
            self.assertTrue('BLOCKER' in win.status.text())
        finally:
            win.close()

    def test_sections_at_minimum_window_size(self):
        win = Window()
        win.resize(840, 590)
        win.show()
        self.app.processEvents()
        try:
            self.assertEqual(len(win.findChildren(QFrame, 'sectionCard')), 3)
            self.assertEqual(win.map_table.height(), 255)
            self.assertEqual(win.report_text.height(), 116)
        finally:
            win.close()


if __name__ == '__main__':
    unittest.main()
