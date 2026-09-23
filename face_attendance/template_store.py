"""Versioned identity templates: equally-sized embeddings are not interchangeable."""
import pickle
from pathlib import Path

TEMPLATE_MODEL = 'opencv-sface-2021dec-v1'
TEMPLATE_VERSION = 2


def read_templates(path, allow_legacy=False):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open('rb') as handle:
        payload = pickle.load(handle)  # Local, trusted enrollment files only.
    if not isinstance(payload, dict):
        raise ValueError('Invalid face template file')
    if payload.get('schema_version') == TEMPLATE_VERSION and payload.get('model') == TEMPLATE_MODEL:
        samples = payload.get('samples')
        if not isinstance(samples, dict):
            raise ValueError('Invalid SFace samples dictionary')
        return samples
    if allow_legacy and all(isinstance(value, (list, tuple)) for value in payload.values()):
        return payload
    raise ValueError('Incompatible face templates. Re-enroll with SFace or migrate saved enrollment photos; '
                     'dlib vectors cannot be converted or mixed with SFace.')


def template_payload(samples):
    return {'schema_version': TEMPLATE_VERSION, 'model': TEMPLATE_MODEL, 'samples': samples}
