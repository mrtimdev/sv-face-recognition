"""Versioned identity templates: equally-sized embeddings are not interchangeable."""
import io
import pickle
from pathlib import Path

import numpy as np

TEMPLATE_MODEL = 'opencv-sface-2021dec-v1'
TEMPLATE_VERSION = 2
MAX_TEMPLATE_BYTES = 256 * 1024 * 1024


def _legacy_bytes(value, encoding='latin1', errors='strict'):
    # NumPy pickles written with protocols 0-2 encode their raw array bytes
    # through this global. Never resolve or expose the general codecs registry.
    if not isinstance(value, str) or encoding not in ('latin1', 'latin-1') or errors != 'strict':
        raise pickle.UnpicklingError('Unsupported legacy array byte encoding')
    return value.encode('latin1')


class _NumericTemplateUnpickler(pickle.Unpickler):
    """Resolve only NumPy's numeric pickle primitives, never arbitrary globals."""
    def find_class(self, module, name):
        core = np.core
        allowed = {
            ('numpy', 'ndarray'): np.ndarray,
            ('numpy', 'dtype'): np.dtype,
            ('_codecs', 'encode'): _legacy_bytes,
        }
        for prefix in ('numpy.core', 'numpy._core'):
            allowed[(prefix + '.multiarray', '_reconstruct')] = core.multiarray._reconstruct
            allowed[(prefix + '.multiarray', 'scalar')] = core.multiarray.scalar
            allowed[(prefix + '.numeric', '_frombuffer')] = core.numeric._frombuffer
        try:
            return allowed[(module, name)]
        except KeyError:
            raise pickle.UnpicklingError(f'Unsupported template global: {module}.{name}') from None

    def persistent_load(self, pid):
        raise pickle.UnpicklingError('Persistent template references are not supported')


def _validate_numeric_payload(payload):
    # Object arrays may contain nested objects even when no forbidden global
    # was required to construct them. Validate the complete graph, not just
    # the samples field; avoid recursion on cyclic or deeply nested pickles.
    pending, visited = [payload], set()
    while pending:
        item = pending.pop()
        if isinstance(item, (dict, list, tuple, set, frozenset)):
            identity = id(item)
            if identity in visited:
                continue
            visited.add(identity)
        if isinstance(item, (np.ndarray, np.generic, np.dtype)):
            dtype = item if isinstance(item, np.dtype) else item.dtype
            if dtype.hasobject or dtype.kind not in 'biufc':
                raise ValueError('Face templates must contain numeric arrays only')
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)



def read_template_bundle(path, allow_legacy=False):
    """Read vectors and photo references from the same atomic catalog version."""
    path = Path(path)
    if not path.exists():
        return {}, {}
    with path.open('rb') as handle:
        if path.stat().st_size > MAX_TEMPLATE_BYTES:
            raise ValueError('Face template file exceeds the size limit')
        data = handle.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        raise ValueError('Face template file exceeds the size limit')
    try:
        payload = _NumericTemplateUnpickler(io.BytesIO(data)).load()
    except (pickle.UnpicklingError, EOFError, AttributeError, TypeError) as exc:
        raise ValueError('Invalid or unsafe face template file') from exc
    _validate_numeric_payload(payload)
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
