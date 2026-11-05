"""Do an employee's angle photos all show the same face?

Only the outcome leaves this machine: HRM stores it and shows it on its angle cards, because HRM has
no face model of its own. A warning asks a person to look; nothing is removed or blocked here.
"""
import numpy as np

# SFace cosine distance. Measured on real angles: 0.13-0.24 for one person across poses, and over
# 0.8 for a different person. OpenCV's published same-person cutoff for this model is about 0.64.
MATCH_DISTANCE = 0.6

WARNING_COLOR = "#c77700"  # Readable on both the light and dark themes.

MATCHED = "matched"
NOT_COMPARED = "not_compared"  # Fine, but nothing to compare it with.
MISMATCH = "mismatch"
NO_FACE = "no_face"
MANY_FACES = "many_faces"
UNREADABLE = "unreadable"

WARNINGS = {
    MISMATCH: "This looks like a different person from the other angles.",
    NO_FACE: "No face found in this photo.",
    MANY_FACES: "More than one face in this photo.",
    UNREADABLE: "This photo can't be read.",
}
SHORT_WARNINGS = {MISMATCH: "Different face", NO_FACE: "No face", MANY_FACES: "Many faces", UNREADABLE: "Unreadable"}
_PROBLEMS = {"No face found in the image": NO_FACE, "More than one face in the image": MANY_FACES}


def is_warning(status):
    return status in WARNINGS


def status_for_problem(problem):
    """The check result for ``angle_templates.encode_angle``'s reason an image has no template."""
    return _PROBLEMS.get(problem, UNREADABLE)


def same_face(first, second):
    return float(1 - np.dot(first, second)) <= MATCH_DISTANCE


def compare_with_each_other(vectors, order=()):
    """{angle: MATCHED, MISMATCH or NOT_COMPARED} for {angle: normalized template}.

    Angles are linked when they look alike, so a side view counts through the front view even if it
    doesn't resemble the opposite side. The largest group is taken to be the person; on a tie, the
    one holding the angle earliest in ``order`` (front view first).
    """
    keys = sorted(vectors, key=lambda key: (order.index(key) if key in order else len(order), key))
    if len(keys) < 2:
        return {key: NOT_COMPARED for key in keys}
    group = {key: index for index, key in enumerate(keys)}
    for index, first in enumerate(keys):
        for second in keys[index + 1:]:
            if group[first] != group[second] and same_face(vectors[first], vectors[second]):
                old, new = group[second], group[first]
                group = {key: new if value == old else value for key, value in group.items()}
    sizes = {}
    for value in group.values():
        sizes[value] = sizes.get(value, 0) + 1
    person = max(sizes, key=lambda value: (sizes[value], -min(i for i, k in enumerate(keys) if group[k] == value)))
    return {key: MATCHED if group[key] == person else MISMATCH for key in keys}
