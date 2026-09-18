"""Shared bounding-box geometry (top, right, bottom, left)."""
import math


def iou(a, b):
    height = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    width = max(0, min(a[1], b[1]) - max(a[3], b[3]))
    intersection = height * width
    area_a = max(0, a[2] - a[0]) * max(0, a[1] - a[3])
    area_b = max(0, b[2] - b[0]) * max(0, b[1] - b[3])
    return intersection / max(1.0, area_a + area_b - intersection)


def association_cost(a, b):
    size = max(1, a[1] - a[3], a[2] - a[0], b[1] - b[3], b[2] - b[0])
    distance = math.hypot((a[1] + a[3] - b[1] - b[3]) / 2,
                          (a[0] + a[2] - b[0] - b[2]) / 2) / size
    overlap = iou(a, b)
    if distance > 0.65 or (overlap < 0.12 and distance > 0.35):
        return float("inf")
    return 1 - overlap + 0.5 * distance


def clamp_box(box, width, height):
    top, right, bottom, left = box
    return (max(0, min(height - 1, top)), max(1, min(width, right)),
            max(1, min(height, bottom)), max(0, min(width - 1, left)))
