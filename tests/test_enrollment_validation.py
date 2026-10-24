"""Enrollment integrity and failure handling; no camera or employee data."""
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance.enrollment import EnrollmentService, read_encodings, write_encodings
from face_attendance.enrollment_photos import photo_directory, sample_photos
from face_attendance.catalog import load_employee_map


def vector(index=0):
    return np.eye(1,128,index,dtype=np.float32)[0]


def image(value=1):
    return np.full((160,160,3), value, np.uint8)


class Backend:
    def __init__(self):
        self.detections = 0
        self.boxes = [(10,150,150,10)]
        self.embedding = vector()

    def face_locations(self, rgb, model=None):
        self.detections += 1
        self.rgb = rgb
        return self.boxes

    def face_encodings(self, rgb, boxes):
        assert rgb is self.rgb, 'Detection and encoding must share the RGB image'
        return [self.embedding]


class EnrollmentValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root/'faces.pickle'
        self.mapping = self.root/'employees.json'
        self.backend = Backend()
        self.service = EnrollmentService(self.catalog,self.mapping,backend=self.backend,check_quality=False)

    def test_one_detection_per_sample_and_normalized_storage(self):
        self.backend.embedding = vector()*5
        outcome = self.service.enroll(image(),'Alice','A')
        self.assertTrue(outcome.ok,outcome.message)
        self.assertEqual(self.backend.detections,1)
        np.testing.assert_allclose(read_encodings(self.catalog)['Alice'][0],vector())

    def test_invalid_images_do_not_initialize_models_or_write_files(self):
        for frame in (None, np.zeros((0,0,3),np.uint8), np.zeros((20,20),np.uint8),
                      np.zeros((20,20,4),np.uint8), np.zeros((20,20,3),np.float32), 'bad'):
            with self.subTest(frame=type(frame)):
                self.assertFalse(self.service.enroll(frame,'Alice').ok)
        self.assertEqual(self.backend.detections,0)
        self.assertFalse(self.catalog.exists())

    def test_partial_faces_and_invalid_embeddings_are_always_rejected(self):
        for box in ((-1,150,150,10),(10,170,150,10),(20,10,10,20),(float('nan'),150,150,10)):
            self.backend.boxes = [box]
            self.assertEqual(self.service.inspect(image())[0],'low_quality')
        self.backend.boxes = [(10,150,150,10)]
        for embedding in (np.zeros(128), np.ones(64), np.ones((1,128)),
                          np.full(128,np.nan), np.full(128,np.inf)):
            self.backend.embedding = embedding
            self.assertEqual(self.service.inspect(image())[0],'error')
        self.assertFalse(self.catalog.exists())

    def test_mixed_batch_rejects_other_identity_without_saving_its_photo(self):
        outcomes = [('ok',vector(),()),('ok',vector(1),()),('ok',vector(),())]
        with patch.object(self.service,'_prepare',side_effect=outcomes):
            result = self.service.enroll_many([image(10),image(20),image(30)],'Alice','A')
        self.assertTrue(result.ok)
        self.assertEqual(result.samples_added,2)
        self.assertIn('sample 2',result.issues[0])
        self.assertEqual([int(cv2.imread(str(p))[0,0,0]) for p in sample_photos(self.catalog,'Alice')],[10,30])

    def test_existing_identity_and_alias_cannot_be_contaminated(self):
        self.assertTrue(self.service.enroll(image(),'Alice','A').ok)
        before = self.catalog.read_bytes()
        self.backend.embedding = vector(1)
        self.assertEqual(self.service.enroll(image(),'Alice','A').status,'identity_mismatch')
        self.assertEqual(self.service.enroll(image(),'Alias','A').status,'identity_mismatch')
        self.assertEqual(self.catalog.read_bytes(),before)
        self.assertNotIn('Alias',load_employee_map(self.mapping))

    def test_failed_catalog_save_restores_mapping_exactly_and_removes_photos(self):
        for existing in (False,True):
            with self.subTest(existing=existing):
                original = b'{"Bob":{"employee_id":"B","name":"Bob"}}\n'
                if existing:
                    self.mapping.write_bytes(original)
                with patch('face_attendance.enrollment.write_encodings',side_effect=OSError('disk full')):
                    result = self.service.enroll(image(),'Alice','A')
                self.assertEqual(result.status,'error')
                self.assertFalse(self.catalog.exists())
                if existing:
                    self.assertEqual(self.mapping.read_bytes(),original)
                else:
                    self.assertFalse(self.mapping.exists())
                self.assertEqual(list(photo_directory(self.catalog).glob('*.jpg')),[])

    def test_concurrent_services_preserve_both_profiles_and_photo_links(self):
        barrier = threading.Barrier(2)
        def prepare(*args):
            barrier.wait(2)
            return 'ok',vector(),()
        def enroll(name):
            service = EnrollmentService(self.catalog,self.mapping,backend=Backend(),check_quality=False)
            def distinct_prepare(*args):
                status,_,issues = prepare(*args)
                return status,vector(0 if name == 'Alice' else 1),issues
            service._prepare = distinct_prepare
            return service.enroll(image(),name,name)
        from face_attendance.enrollment import save_sample_photo
        def slow_photo(*args):
            time.sleep(.03)
            return save_sample_photo(*args)
        with patch('face_attendance.enrollment.save_sample_photo',side_effect=slow_photo), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(enroll,['Alice','Bob']))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(set(read_encodings(self.catalog)),{'Alice','Bob'})
        self.assertEqual(set(load_employee_map(self.mapping)),{'Alice','Bob'})
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))),2)

    def test_setup_limits_are_checked_before_analysis(self):
        self.assertEqual(self.service.enroll_many([image()]*65,'Alice').status,'invalid_samples')
        self.assertFalse(self.service.enroll(image(),'A'*101).ok)
        self.assertFalse(self.service.enroll(image(),'A\nB').ok)
        self.assertFalse(self.service.enroll(image(),'Alice','A\tB').ok)
        self.assertEqual(self.backend.detections,0)

    def test_concurrent_id_assignment_cannot_silently_reassign_label(self):
        barrier = threading.Barrier(2)
        def prepare(*args):
            barrier.wait(2)
            return 'ok',vector(),()
        def enroll(employee_id):
            service = EnrollmentService(self.catalog,self.mapping,backend=Backend(),check_quality=False)
            service._prepare = prepare
            return service.enroll(image(),'Alice',employee_id)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(enroll,['A','B']))
        self.assertEqual(sorted(result.status for result in results),['enrolled','mapped_conflict'])
        self.assertEqual(len(read_encodings(self.catalog)['Alice']),1)
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))),1)
        winner = next(result for result in results if result.ok)
        self.assertEqual(load_employee_map(self.mapping)['Alice'].employee_id,winner.employee_id)

    def test_mapping_failure_after_replace_restores_previous_state(self):
        from face_attendance.enrollment import write_employee
        original = b'{"Bob":{"employee_id":"B","name":"Bob"}}\n'
        self.mapping.write_bytes(original)
        def interrupted(*args):
            write_employee(*args)
            raise OSError('interrupted after mapping write')
        with patch('face_attendance.enrollment.write_employee',side_effect=interrupted):
            result = self.service.enroll(image(),'Alice','A')
        self.assertFalse(result.ok)
        self.assertEqual(self.mapping.read_bytes(),original)
        self.assertFalse(self.catalog.exists())
        self.assertEqual(list(photo_directory(self.catalog).glob('*.jpg')),[])

    def test_real_bundled_model_can_enroll_and_append_same_person(self):
        from face_attendance.samples import SAMPLES
        frame = cv2.imread(str(SAMPLES[0].path))
        service = EnrollmentService(self.catalog,self.mapping)
        result = service.enroll(frame,'Sample','S')
        self.assertTrue(result.ok,result.message)
        variant = cv2.convertScaleAbs(frame, alpha=.97, beta=3)
        result = service.enroll(variant,'Sample','S')
        self.assertTrue(result.ok,result.message)
        self.assertEqual(result.total_samples,2)
