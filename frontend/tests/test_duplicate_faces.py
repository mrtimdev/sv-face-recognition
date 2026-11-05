"""Cross-owner face rejection, existing-conflict scans, aliases and API finalization."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np

from face_attendance.catalog import legacy_employee_id, load_employee_map
from face_attendance.enrollment import EnrollmentService, read_encodings, write_encodings, write_employee
from face_attendance.face_duplicates import FaceDuplicateIndex
from face_attendance.enrollment_photos import photo_directory
from face_attendance.models import Employee
from tests.test_enrollment_validation import Backend, image, vector


class DuplicateFacesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog, self.mapping = self.root/'faces.pickle', self.root/'employees.json'
        self.backend = Backend()
        self.service = EnrollmentService(self.catalog,self.mapping,backend=self.backend,check_quality=False)
        self.assertTrue(self.service.enroll(image(),'Tim Dev','EMP002').ok)

    def test_new_name_and_optional_id_cannot_bypass_duplicate_face_rejection(self):
        before = self.catalog.read_bytes(), self.mapping.read_bytes()
        for employee_id in (None,'EMP999'):
            result = self.service.enroll(image(),'Tim Dev 2',employee_id)
            self.assertEqual(result.status,'duplicate_face')
            self.assertIn('Tim Dev (EMP002)',result.message)
            self.assertIn('Update photo',result.message)
            self.assertEqual(result.conflicts[0].matched_label,'Tim Dev')
        self.assertEqual((self.catalog.read_bytes(),self.mapping.read_bytes()),before)
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))),1)

    def test_cosine_match_rejects_nonidentical_and_scaled_vectors(self):
        self.backend.embedding = vector()*.95 + vector(1)*.15
        result = self.service.enroll(image(),'Tim Dev 2','EMP999')
        self.assertEqual(result.status,'duplicate_face')
        self.assertGreater(result.conflicts[0].distance,0)
        self.assertLess(result.conflicts[0].distance,.05)

    def test_updates_same_id_aliases_and_different_people_still_work(self):
        self.assertTrue(self.service.enroll(image(),'Tim Dev','EMP002').ok)
        self.assertTrue(self.service.enroll(image(),'Tim Alias','EMP002').ok)
        self.assertEqual(self.service.duplicate_faces(),[])
        self.backend.embedding = vector(1)
        self.assertTrue(self.service.enroll(image(),'Another employee','EMP003').ok)
        self.assertEqual(self.service.duplicate_faces(),[])

    def test_any_duplicate_blocks_entire_batch_before_photo_or_mapping_writes(self):
        with patch.object(self.service,'_prepare',side_effect=[('ok',vector(1),()),('ok',vector(),())]):
            result = self.service.enroll_many([image(10),image(20)],'Tim Dev 2','EMP999')
        self.assertEqual(result.status,'duplicate_face')
        self.assertNotIn('Tim Dev 2',read_encodings(self.catalog))
        self.assertNotIn('Tim Dev 2',load_employee_map(self.mapping))
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))),1)

    def test_existing_profiles_are_reported_without_any_catalog_mutation(self):
        data = read_encodings(self.catalog)
        data['Tim Dev 2'] = [vector()*.95+vector(1)*.15]
        data['Tim Alias'] = [vector()]
        write_encodings(self.catalog,data)
        write_employee(self.mapping,'Tim Alias','EMP002')
        before = self.catalog.read_bytes(), self.mapping.read_bytes()
        matches = self.service.duplicate_faces()
        self.assertEqual({(m.label,m.matched_label) for m in matches},
                         {('Tim Alias','Tim Dev 2'),('Tim Dev','Tim Dev 2')})
        self.assertTrue(all(legacy_employee_id('Tim Dev 2') in (m.employee_id,m.matched_employee_id) for m in matches))
        self.assertEqual((self.catalog.read_bytes(),self.mapping.read_bytes()),before)

    def test_assigning_first_permanent_id_to_legacy_profile_is_allowed(self):
        self.backend.embedding = vector(1)
        self.assertTrue(self.service.enroll(image(),'Legacy').ok)
        self.assertTrue(self.service.enroll(image(),'Legacy','EMP003').ok)
        self.assertEqual(load_employee_map(self.mapping)['Legacy'].employee_id,'EMP003')

    def test_close_sample_under_another_existing_employee_is_blocked(self):
        self.backend.embedding = vector(1)
        self.assertTrue(self.service.enroll(image(),'Other','EMP003').ok)
        before = self.catalog.read_bytes()
        self.backend.embedding = vector()
        result = self.service.enroll(image(),'Other','EMP003')
        self.assertEqual(result.status,'duplicate_face')
        self.assertEqual(self.catalog.read_bytes(),before)

    def test_configured_tolerance_and_nearest_owner_are_used(self):
        index = FaceDuplicateIndex({'One':[vector()], 'Two':[vector(1)]},
                                   {'One':Employee('A','Alice'),'Two':Employee('B','Bob')})
        candidate = vector()*.8+vector(1)*.6
        self.assertIsNone(index.match(candidate,'C',threshold=.1))
        match = index.match(candidate,'C',threshold=.3)
        self.assertEqual(match.matched_employee_name,'Alice')
        self.assertAlmostEqual(match.distance,.2,places=6)
        self.assertEqual(index.match(candidate,'C',threshold=match.distance).matched_employee_id,'A')

    def test_invalid_old_vectors_do_not_produce_false_duplicates(self):
        index = FaceDuplicateIndex({'Broken':[np.zeros(128),np.ones(64),np.full(128,np.nan)],
                                    'Valid':[vector()]}, {})
        self.assertEqual(index.conflicts(),[])
        self.assertIsNone(index.match(vector(1),'new'))

    def test_two_simultaneous_enrollments_cannot_publish_same_face_twice(self):
        # A new face (orthogonal to Tim) is submitted under two different IDs.
        barrier = threading.Barrier(2)
        def enroll(name):
            service = EnrollmentService(self.catalog,self.mapping,check_quality=False)
            def prepare(*args):
                barrier.wait(2)
                return 'ok',vector(1),()
            service._prepare = prepare
            return service.enroll(image(),name,name)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(enroll,['First','Second']))
        self.assertEqual(sorted(r.status for r in results),['duplicate_face','enrolled'])
        self.assertEqual(len(read_encodings(self.catalog)),2)
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))),2)

    def test_scan_covers_both_sides_of_comparison_block_boundary(self):
        samples = {f'Alias{i}':[vector()] for i in range(258)}
        records = {name:Employee('same','Tim') for name in samples}
        samples['Different'] = [vector()]
        matches = FaceDuplicateIndex(samples,records).conflicts()
        self.assertEqual(len(matches),258)
        self.assertTrue(all('Different' in (m.label,m.matched_label) for m in matches))

    def test_real_sface_rejects_same_person_under_second_name(self):
        import cv2
        from face_attendance.samples import SAMPLES
        service = EnrollmentService(self.root/'real.pickle',self.root/'real.json')
        original = cv2.imread(str(SAMPLES[0].path))
        self.assertTrue(service.enroll(original,'Original','REAL1').ok)
        changed = cv2.convertScaleAbs(original,alpha=.97,beta=3)
        result = service.enroll(changed,'Second name','REAL2')
        self.assertEqual(result.status,'duplicate_face',result.message)
        self.assertEqual(result.conflicts[0].matched_employee_id,'REAL1')

    def test_cli_rejects_second_profile_with_named_message(self):
        import io
        from contextlib import redirect_stdout
        import enroll_faces
        before = self.catalog.read_bytes(),self.mapping.read_bytes()
        output = io.StringIO()
        with patch.object(enroll_faces,'ENCODINGS_PATH',str(self.catalog)), \
                patch.object(enroll_faces,'EMPLOYEES_PATH',self.mapping), \
                patch.object(enroll_faces,'get_backend',return_value=self.backend), \
                patch.object(enroll_faces,'read_image',return_value=image()), redirect_stdout(output):
            enroll_faces.enroll('Tim Dev 2','photo.jpg','EMP999')
        self.assertIn('Face already matches Tim Dev (EMP002)',output.getvalue())
        self.assertEqual((self.catalog.read_bytes(),self.mapping.read_bytes()),before)
