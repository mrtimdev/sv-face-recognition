"""Original enrollment sample photos, referenced atomically by the face catalog."""
import hashlib
import logging
from pathlib import Path
import re
from uuid import uuid4

from .storage import SnapshotService
from .template_store import read_template_bundle


def photo_directory(encodings_path):
    return Path(encodings_path).parent / 'enrollment_photos' / 'samples'


def safe_label(label):
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(label))[:60]


def portrait_name(label):
    """File name of the directory portrait the dashboard saves for an enrollment label."""
    return f"{safe_label(label)}_{hashlib.sha256(str(label).encode('utf-8')).hexdigest()[:12]}.jpg"


def portrait_path(encodings_path, label, labels=()):
    """The existing portrait for *label*: the exact name, or an unambiguous legacy one."""
    directory = Path(encodings_path).parent / 'enrollment_photos'
    path = directory / portrait_name(label)
    if path.is_file():
        return path
    legacy = directory / f"{safe_label(label)}.jpg"
    same = sum(safe_label(other).casefold() == safe_label(label).casefold() for other in labels)
    return legacy if same <= 1 and legacy.is_file() else None


def photo_path(encodings_path, reference):
    # A catalog reference is a generated basename, never an arbitrary local path.
    if not isinstance(reference, str) or not re.fullmatch(r'[0-9a-f]{32}\.jpg', reference):
        return None
    root = photo_directory(encodings_path)
    path = root / reference
    if path.is_symlink() or path.resolve().parent != root.resolve():
        return None
    return path


def save_sample_photo(encodings_path, image):
    directory = photo_directory(encodings_path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (uuid4().hex + '.jpg')
    SnapshotService._write_image(path, image)
    return path


def remove_sample_photos(encodings_path, references):
    """Catalog commit comes first; cleanup failure must not misreport a rollback."""
    for reference in set(references):
        path = photo_path(encodings_path, reference)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.warning('Could not remove enrollment sample photo: %s', path, exc_info=True)


def sample_photo_map(encodings_path):
    _, refs = read_template_bundle(encodings_path)
    return {label: [photo_path(encodings_path, ref) for ref in values] for label, values in refs.items()}


def sample_photos(encodings_path, label):
    return sample_photo_map(encodings_path).get(label, [])
