"""Do an employee's angle photos all show the same face?

Only the outcome leaves this machine: HRM stores it and shows it on its angle cards, because HRM has
no face model of its own. A flagged angle stays in HRM for a person to look at, but the Live Monitor
doesn't match against it (see angle_templates.recognition_vectors).
"""
from dataclasses import dataclass

import numpy as np

# SFace cosine distance. Measured on real angles: 0.13-0.24 for one person across poses, and over
# 0.8 for a different person. OpenCV's published same-person cutoff for this model is about 0.64.
MATCH_DISTANCE = 0.6

WARNING_COLOR = "#c77700"  # Readable on both the light and dark themes.

MATCHED = "matched"
NOT_COMPARED = "not_compared"  # Fine, but nothing to compare it with.
MISMATCH = "mismatch"
PROFILE_MISMATCH = "profile_mismatch"
NO_FACE = "no_face"
MANY_FACES = "many_faces"
UNREADABLE = "unreadable"

WARNINGS = {
    MISMATCH: "This looks like a different person from the other angles.",
    PROFILE_MISMATCH: "This doesn't match the employee's profile photo.",
    NO_FACE: "No face found in this photo.",
    MANY_FACES: "More than one face in this photo.",
    UNREADABLE: "This photo can't be read.",
}
SHORT_WARNINGS = {MISMATCH: "Different face", PROFILE_MISMATCH: "Not profile face", NO_FACE: "No face",
                  MANY_FACES: "Many faces", UNREADABLE: "Unreadable"}
_PROBLEMS = {"No face found in the image": NO_FACE, "More than one face in the image": MANY_FACES}


REQUIRED_ANGLES = 6


@dataclass(frozen=True)
class Score:
    """How many of an employee's required angles passed. Missing ones count as neither."""
    verified: int
    warnings: int
    total: int = REQUIRED_ANGLES

    @property
    def verified_percent(self):
        return round(self.verified * 100 / self.total)

    @property
    def warning_percent(self):
        return round(self.warnings * 100 / self.total)

    @property
    def recognizable(self):
        """The Live Monitor may use this employee only when verified angles outnumber flagged ones."""
        return self.verified > self.warnings


def score(statuses, total=REQUIRED_ANGLES):
    statuses = list(statuses)
    return Score(sum(1 for status in statuses if status == MATCHED), sum(1 for status in statuses if is_warning(status)),
                 total)


def is_warning(status):
    return status in WARNINGS


def status_for_problem(problem):
    """The check result for ``angle_templates.encode_angle``'s reason an image has no template."""
    return _PROBLEMS.get(problem, UNREADABLE)


def same_face(first, second):
    return float(1 - np.dot(first, second)) <= MATCH_DISTANCE


def compare_with_profile(vectors, profile):
    """{angle: MATCHED or PROFILE_MISMATCH}: is each angle linked to the profile photo?

    Linking lets a side view count through the front view, which a frontal profile photo may not
    resemble directly. Angles that only match each other still fail.
    """
    linked, waiting = [profile], dict(vectors)
    while linked and waiting:
        current = linked.pop()
        for key in [key for key, vector in waiting.items() if same_face(current, vector)]:
            linked.append(waiting.pop(key))
    return {key: PROFILE_MISMATCH if key in waiting else MATCHED for key in vectors}


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
