"""numpy BGR frames to Qt images, without dangling buffers.

``QImage`` can wrap a numpy buffer without copying, so the result is copied
before the engine reuses its render canvas. Widgets therefore always own a
stable image, and no frame is ever deep-copied twice per display.
"""
import numpy as np
from PyQt6.QtGui import QImage, QPixmap


def to_qimage(frame):
    if frame is None or not hasattr(frame, "shape") or frame.size == 0:
        return QImage()
    if not frame.flags["C_CONTIGUOUS"]:
        frame = np.ascontiguousarray(frame)
    if frame.ndim == 2:
        height, width = frame.shape
        image = QImage(frame.data, width, height, width, QImage.Format.Format_Grayscale8)
    else:
        height, width, channels = frame.shape
        if channels != 3:
            return QImage()
        image = QImage(frame.data, width, height, channels * width, QImage.Format.Format_BGR888)
    return image.copy()


def to_pixmap(frame):
    return QPixmap.fromImage(to_qimage(frame))
