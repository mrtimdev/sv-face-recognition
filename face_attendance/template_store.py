"""Versioned identity templates: equally-sized embeddings are not interchangeable."""
import pickle
from pathlib import Path

import numpy as np

TEMPLATE_MODEL = 'opencv-sface-2021dec-v1'
TEMPLATE_VERSION = 2


def read_template_bundle(path, allow_legacy=False):
    """Read vectors and photo references from the same atomic catalog version."""
    path = Path(path)
    if not path.exists():
        return {}, {}
    with path.open('rb') as handle:
        payload = pickle.load(handle)  # Local, trusted enrollment files only.
    if not isinstance(payload, dict):
        raise ValueError('Invalid face template file')
    if payload.get('schema_version') == TEMPLATE_VERSION and payload.get('model') == TEMPLATE_MODEL:
        samples = payload.get('samples')
        if not isinstance(samples, dict):
            raise ValueError('Invalid SFace samples dictionary')
        photos = payload.get('sample_photos', {})
        return samples, aligned_photo_references(samples, photos if isinstance(photos, dict) else {})
    if allow_legacy and all(isinstance(value, (list, tuple)) for value in payload.values()):
        return payload, aligned_photo_references(payload, {})
    raise ValueError('Incompatible face templates. Re-enroll with SFace or migrate saved enrollment photos; '
                     'dlib vectors cannot be converted or mixed with SFace.')


def read_templates(path, allow_legacy=False):
    return read_template_bundle(path, allow_legacy)[0]


def read_photo_references(path):
    return read_template_bundle(path)[1]


def aligned_photo_references(samples, photos):
    result = {}
    for label, vectors in samples.items():
        refs = photos.get(label, [])
        if not isinstance(refs, (list, tuple)):
            refs = []
        result[label] = [(refs[i] if i < len(refs) and isinstance(refs[i], str) else None)
                         for i in range(len(vectors))]
    return result


def preserve_photo_references(path, samples):
    """Legacy writers retain photos only for identical, still-present vectors."""
    previous, photos = read_template_bundle(path)
    key = lambda vector: (np.asarray(vector).dtype.str, np.asarray(vector).shape,
                          np.asarray(vector).tobytes())
    result = {}
    for label, vectors in samples.items():
        pool = {}
        for vector, photo in zip(previous.get(label, []), photos.get(label, [])):
            pool.setdefault(key(vector), []).append(photo)
        result[label] = []
        for vector in vectors:
            matches = pool.get(key(vector), [])
            result[label].append(matches.pop(0) if matches else None)
    return result


def template_payload(samples, photos=None):
    payload = {'schema_version': TEMPLATE_VERSION, 'model': TEMPLATE_MODEL, 'samples': samples}
    if photos is not None:
        payload['sample_photos'] = aligned_photo_references(samples, photos)
    return payload
