"""Sample photos shipped with the app, for trying liveness without real employees.

Three public upstream MiniFASNet examples (Apache-2.0, see
``assets/samples/NOTICE.md``): a real face, a printed photo and a phone screen.

* ``check_samples()`` runs the bundled detector and anti-spoof models on them,
  the way Live Monitor judges a face, so an installation can prove its
  liveness check works before anyone is enrolled.
* The optional demo employee is enrolled from the real-face sample.  Holding a
  printed or on-screen copy of a sample up to the camera then shows a known
  face being recognised but blocked as a photo.
"""
from dataclasses import dataclass
from pathlib import Path

import cv2

from .storage import read_image

SAMPLES_DIR = Path(__file__).resolve().parent / "assets" / "samples"
DEMO_LABEL = "Demo employee (sample)"
DEMO_EMPLOYEE_ID = "DEMO-0001"


@dataclass(frozen=True)
class Sample:
    key: str
    title: str
    filename: str
    expected_live: bool

    @property
    def path(self):
        return SAMPLES_DIR / self.filename


SAMPLES = (
    Sample("live", "Real face", "live.jpg", True),
    Sample("print", "Printed photo", "print.jpg", False),
    Sample("screen", "Phone screen", "screen.jpg", False),
)


@dataclass(frozen=True)
class SampleResult:
    sample: Sample
    live_score: object = None      # the weaker model's live probability, or None
    judged_live: bool = False
    error: str = ""

    @property
    def correct(self):
        return not self.error and self.judged_live == self.sample.expected_live


def check_samples(detector=None, model=None, detection_scale=0.5):
    """Judge each sample like Live Monitor does; returns one ``SampleResult`` each."""
    from .anti_spoof import AntiSpoofService, live_sample
    if detector is None:
        from .face_backend import OpenCVFaceBackend
        detector = OpenCVFaceBackend()
    model = model or AntiSpoofService()
    results = []
    for sample in SAMPLES:
        frame = read_image(sample.path)
        if frame is None:
            results.append(SampleResult(sample, error="The sample photo is missing from this installation."))
            continue
        # Detect at the dashboard's scale; judge the original pixels.
        small = cv2.resize(frame, (max(1, round(frame.shape[1] * detection_scale)),
                                   max(1, round(frame.shape[0] * detection_scale))))
        boxes = detector.face_locations(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        if len(boxes) != 1:
            results.append(SampleResult(sample, error="The face in the sample photo wasn't found."))
            continue
        sy, sx = frame.shape[0] / small.shape[0], frame.shape[1] / small.shape[1]
        top, right, bottom, left = boxes[0]
        inspected = model.inspect(frame, (top * sy, right * sx, bottom * sy, left * sx))
        if inspected["error"]:
            results.append(SampleResult(sample, error=inspected["error"]))
            continue
        score = inspected["live_score"]
        results.append(SampleResult(sample, score, live_sample(score)))
    return results


def has_demo_employee(encodings_path):
    from .template_store import read_templates
    try:
        return DEMO_LABEL in read_templates(encodings_path)
    except (OSError, ValueError):
        return False


def add_demo_employee(encodings_path, employees_path, backend=None):
    """Enroll the real-face sample as a demo employee; returns the ``EnrollmentOutcome``."""
    from .enrollment import EnrollmentService
    image = read_image(SAMPLES[0].path)
    if image is None:
        raise ValueError("The sample photo is missing from this installation.")
    service = EnrollmentService(encodings_path, employees_path, backend=backend, check_quality=False)
    return service.enroll_many([image], DEMO_LABEL, DEMO_EMPLOYEE_ID)


def remove_demo_employee(encodings_path, employees_path):
    """Remove the demo employee (attendance history is kept, as for any employee)."""
    from .enrollment import EnrollmentService
    return EnrollmentService(encodings_path, employees_path).delete_employee(DEMO_LABEL)
