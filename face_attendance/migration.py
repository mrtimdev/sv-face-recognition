"""Re-encode explicitly labeled enrollment photos without rewriting legacy data."""
from collections import Counter
from pathlib import Path

import cv2

from .enrollment import EnrollmentService, write_encodings
from .template_store import read_templates


def photo_stem(label):
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in label)[:60]


def migrate_photos(legacy_path, output_path, employees_path, photo_dir=None, backend=None):
    """Create a separate catalog, retaining empty labels that need re-enrollment.

    Only dashboard enrollment photos with an exact, unique filename qualify.
    Attendance captures and name similarity are never used to assign identities.
    Stop the app before running: this is an offline, single-writer migration.
    """
    legacy_path, output_path = Path(legacy_path), Path(output_path)
    if not legacy_path.is_file():
        raise FileNotFoundError(legacy_path)
    if output_path.exists() or output_path.resolve() == legacy_path.resolve():
        raise ValueError('Destination already exists; refusing to overwrite face templates')
    labels = read_templates(legacy_path, allow_legacy=True)
    if any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError('Invalid enrollment label')
    photo_dir = Path(photo_dir) if photo_dir else legacy_path.parent / 'enrollment_photos'
    counts = Counter(photo_stem(label).casefold() for label in labels)
    service = EnrollmentService(output_path, employees_path, backend=backend)
    samples, report = {}, {}
    for label in labels:
        samples[label] = []
        stem = photo_stem(label)
        if counts[stem.casefold()] != 1:
            report[label] = 'Re-enroll: ambiguous enrollment photo filename'
            continue
        path = photo_dir / (stem + '.jpg')
        if not path.is_file():
            report[label] = 'Re-enroll: no saved enrollment photo'
            continue
        frame = cv2.imread(str(path))
        if frame is None:
            report[label] = 'Re-enroll: unreadable enrollment photo'
            continue
        status, value, _ = service._prepare(frame, check_quality=True)
        if status == 'ok':
            samples[label] = [value]
            report[label] = 'Migrated from saved enrollment photo'
        else:
            report[label] = f'Re-enroll: {value}'
    # No changes to employees.json, the old pickle, or attendance databases.
    if output_path.exists():
        raise ValueError('Destination appeared during migration; refusing to overwrite')
    write_encodings(output_path, samples)
    return report
