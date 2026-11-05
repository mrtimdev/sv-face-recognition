"""Angle cards animate a change, and stay still when nothing changed."""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication

from face_attendance.dashboard.angle_image import MARK_MATCHED, MARK_WARNING, AngleImage

APP = QApplication.instance() or QApplication([])


def pixmap(color):
    image = QPixmap(150, 150)
    image.fill(QColor(color))
    return image


def pump(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    return condition()


class AngleImageTests(unittest.TestCase):
    def setUp(self):
        self.image = AngleImage()
        self.image.setFixedSize(150, 150)
        self.addCleanup(self.image.deleteLater)
        self.image.show()

    def _settled(self):
        return pump(lambda: not self.image.animating)

    def test_a_new_image_animates_then_settles_on_that_image(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self.image.animating)
        self.assertTrue(self._settled())
        self.assertEqual(self.image.pixmap().toImage(), pixmap("red").toImage())

    def test_setting_the_same_image_again_stays_still(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self._settled())
        self.image.clear()
        self.image.setText("")
        self.image.setPixmap(pixmap("red"))
        self.assertFalse(self.image.animating)

    def test_a_different_image_animates_from_the_old_one(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self._settled())
        self.image.setPixmap(pixmap("blue"))
        self.assertTrue(self.image.animating)
        self.assertIsNotNone(self.image._outgoing)
        self.assertTrue(self._settled())
        self.assertIsNone(self.image._outgoing)

    def test_removing_an_image_fades_it_out_to_the_text(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self._settled())
        self.image.clear()
        self.image.setText("—")
        self.assertTrue(self.image.animating)
        self.assertTrue(self._settled())
        self.assertEqual(self.image.text(), "—")

    def test_cards_can_start_one_after_another(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self._settled())
        self.image.next_delay_ms = 50
        self.image.setPixmap(pixmap("blue"))
        self.assertTrue(self.image._delay.isActive())
        self.assertEqual(self.image.next_delay_ms, 0)
        self.assertTrue(self._settled())

    def test_nothing_animates_off_screen(self):
        self.image.hide()
        self.image.setPixmap(pixmap("red"))
        self.image.start_scan()
        self.assertFalse(self.image.animating)
        self.assertFalse(self.image.scanning)
        self.assertEqual(self.image.pixmap().toImage(), pixmap("red").toImage())

    def test_a_new_result_pulses_but_the_same_one_does_not(self):
        self.image.setPixmap(pixmap("red"))
        self.assertTrue(self._settled())
        self.image.set_mark(MARK_MATCHED)
        self.assertTrue(self.image.animating)
        self.assertTrue(self._settled())
        self.image.set_mark(MARK_MATCHED)
        self.assertFalse(self.image.animating)
        self.image.set_mark(MARK_WARNING)
        self.assertTrue(self.image.animating)

    def test_every_stage_paints(self):
        self.image.next_delay_ms = 0
        self.image.start_scan()
        self.assertTrue(self.image.scanning)
        for _ in range(3):
            APP.processEvents()
            self.image.grab()
            time.sleep(0.03)
        self.image.stop_scan()
        self.assertFalse(self.image.scanning)
        self.image.setPixmap(pixmap("red"))
        self.image.set_mark(MARK_WARNING)
        self.image.setPixmap(pixmap("blue"))
        while self.image.animating:
            APP.processEvents()
            self.assertFalse(self.image.grab().isNull())
            time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
