"""Restricted NumPy catalog loading: compatibility without pickle execution."""
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from face_attendance import template_store as store


class Malicious:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return eval, (f"__import__('pathlib').Path({str(self.marker)!r}).write_text('executed')",)


class TemplateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'faces.pickle'

    def write(self, payload, protocol=pickle.HIGHEST_PROTOCOL):
        self.catalog.write_bytes(pickle.dumps(payload, protocol=protocol))

    def test_malicious_reduce_cannot_execute_even_in_legacy_mode(self):
        marker = self.root / 'executed.txt'
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.write({'Alice': [Malicious(marker)]})
                with self.assertRaises(ValueError):
                    store.read_template_bundle(self.catalog, allow_legacy=legacy)
                self.assertFalse(marker.exists())

    def test_numeric_arrays_roundtrip_all_pickle_protocols_and_dtypes(self):
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            for dtype in (np.float32, np.float64, np.int16):
                with self.subTest(protocol=protocol, dtype=dtype):
                    vector = np.arange(128, dtype=dtype)
                    self.write(store.template_payload({'Alice': [vector]}, {'Alice': ['photo.jpg']}), protocol)
                    samples, photos = store.read_template_bundle(self.catalog)
                    np.testing.assert_array_equal(samples['Alice'][0], vector)
                    self.assertEqual(samples['Alice'][0].dtype, vector.dtype)
                    self.assertEqual(photos, {'Alice': ['photo.jpg']})

    def test_new_numpy_core_module_paths_load_on_existing_numpy(self):
        payload = store.template_payload({'Alice': [np.arange(128, dtype=np.float64)]})
        data = pickle.dumps(payload, protocol=2)
        self.assertIn(b'numpy.core.multiarray', data)
        self.catalog.write_bytes(data.replace(b'numpy.core.multiarray', b'numpy._core.multiarray'))
        samples, _ = store.read_template_bundle(self.catalog)
        np.testing.assert_array_equal(samples['Alice'][0], np.arange(128))

    def test_numpy_scalars_and_explicit_legacy_catalog_still_load(self):
        vector = [np.float64(value) for value in range(128)]
        self.write({'Alice': [vector]})
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            store.read_template_bundle(self.catalog)
        samples, _ = store.read_template_bundle(self.catalog, allow_legacy=True)
        np.testing.assert_array_equal(samples['Alice'][0], vector)

    def test_object_arrays_are_refused_in_samples_or_extra_metadata(self):
        for extra in (False, True):
            with self.subTest(extra=extra):
                payload = store.template_payload({'Alice': [np.ones(128)]})
                if extra:
                    payload['extra'] = np.array(['untrusted'], dtype=object)
                else:
                    payload['samples']['Alice'] = [np.array([1] * 128, dtype=object)]
                self.write(payload)
                with self.assertRaisesRegex(ValueError, 'numeric arrays'):
                    store.read_template_bundle(self.catalog)

    def test_size_limit_is_checked_before_unpickling(self):
        self.write(store.template_payload({'Alice': [np.ones(128)]}))
        with patch.object(store, 'MAX_TEMPLATE_BYTES', 20), \
                patch.object(store._NumericTemplateUnpickler, 'load') as load:
            with self.assertRaisesRegex(ValueError, 'size limit'):
                store.read_template_bundle(self.catalog)
            load.assert_not_called()

    def test_unrelated_numpy_callable_is_not_allowed(self):
        class NumpyCallable:
            def __reduce__(self):
                return np.zeros, ((128,),)
        self.write({'Alice': [NumpyCallable()]})
        with self.assertRaises(ValueError):
            store.read_template_bundle(self.catalog, allow_legacy=True)
