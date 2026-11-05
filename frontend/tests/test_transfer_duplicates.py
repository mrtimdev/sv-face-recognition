"""Archive imports enforce cross-employee face ownership before writing photos."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

from face_attendance import transfer
from face_attendance.enrollment import write_encodings, read_encodings


def vector(index):
    result = np.zeros(128)
    result[index] = 1
    return result.tolist()


def entry(label, employee_id, *vectors, name=None):
    return dict(label=label, employee_id=employee_id, name=name or label,
                samples=[dict(vector=value, photo='photos/' + 'a' * 32 + '.jpg')
                         for value in vectors], portrait='portraits/1.jpg')


class TransferDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'faces.pickle'
        self.mapping = self.root / 'employees.json'
        self.archive = self.root / 'employees.zip'

    def seed(self, samples, records):
        write_encodings(self.catalog, samples)
        self.mapping.write_text(json.dumps(records))

    def run_import(self, items, duplicate_distance=.5):
        manifest = dict(format=transfer.FORMAT, version=transfer.FORMAT_VERSION,
                        model=transfer.TEMPLATE_MODEL, employees=items)
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr(transfer.MANIFEST, json.dumps(manifest))
        return transfer.import_employees(self.archive, self.catalog, self.mapping, duplicate_distance=duplicate_distance)

    def test_local_match_skips_whole_entry_before_any_photo_write(self):
        self.seed({'Alice': [vector(0)]}, {'Alice': dict(employee_id='A', name='Alice')})
        before_catalog, before_mapping = self.catalog.read_bytes(), self.mapping.read_bytes()
        with patch.object(transfer, '_write_file') as write_photo:
            report = self.run_import([entry('Bob', 'B', vector(1), vector(0))])
        self.assertFalse(report.changed)
        self.assertIn('Bob: face already enrolled as Alice (ID A)', report.skipped)
        write_photo.assert_not_called()
        self.assertEqual(self.catalog.read_bytes(), before_catalog)
        self.assertEqual(self.mapping.read_bytes(), before_mapping)

    def test_same_archive_cannot_assign_one_face_to_two_employees(self):
        report = self.run_import([entry('Alice', 'A', vector(0)), entry('Bob', 'B', vector(0))])
        self.assertEqual(report.added, ['Alice'])
        self.assertIn('Bob: face already enrolled as Alice (ID A)', report.skipped)
        self.assertEqual(set(read_encodings(self.catalog)), {'Alice'})

    def test_same_employee_alias_and_new_samples_still_merge(self):
        self.seed({'Alice': [vector(0)]}, {'Alice': dict(employee_id='A', name='Alice')})
        report = self.run_import([
            entry('Alice side', 'A', vector(0), name='Alice'),
            entry('Alice', 'A', vector(0), vector(1)),
            entry('Bob', 'B', vector(2)),
        ])
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.updated, ['Alice'])
        self.assertEqual(report.samples, 3)
        self.assertEqual(len(read_encodings(self.catalog)['Alice']), 2)

    def test_archive_alias_uses_preceding_explicit_employee_identity(self):
        report = self.run_import([
            entry('Alice', 'A', vector(0)),
            entry('Alice side', 'A', vector(0), name='Alice'),
        ])
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.samples, 2)
        self.assertEqual(set(read_encodings(self.catalog)), {'Alice', 'Alice side'})

    def test_preexisting_identical_sample_does_not_bypass_conflict_check(self):
        self.seed({'Alice': [vector(0)], 'Bob': [vector(0)]},
                  {'Alice': dict(employee_id='A', name='Alice'),
                   'Bob': dict(employee_id='B', name='Bob')})
        report = self.run_import([entry('Bob', 'B', vector(0))])
        self.assertFalse(report.changed)
        self.assertIn('Bob: face already enrolled as Alice (ID A)', report.skipped)

    def test_import_uses_configured_recognition_tolerance(self):
        self.seed({'Alice': [vector(0)]}, {'Alice': dict(employee_id='A', name='Alice')})
        near = np.asarray(vector(0))*.4 + np.asarray(vector(1))*np.sqrt(1-.4**2)
        report = self.run_import([entry('Bob','B',near.tolist())],duplicate_distance=.7)
        self.assertFalse(report.changed)
        self.assertIn('Alice (ID A)',report.skipped[0])
