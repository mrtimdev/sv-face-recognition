"""Do an employee's angle photos show the same face, and does HRM hear about it?"""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance import angle_checks as ac
from face_attendance import angle_templates as at
from face_attendance.hrm_sync import HRMConfig, HRMError

ORDER = at.ANGLE_ORDER


def unit(*values):
    vector = np.zeros(128)
    vector[:len(values)] = values
    return vector / np.linalg.norm(vector)


PERSON = unit(1, 0.1)
ALSO_PERSON = unit(1, 0.25)
SOMEONE_ELSE = unit(0.05, 1)
THIRD = unit(0, 0, 1)


def entry(vector=None, problem="", sha="s"):
    return {"sha256": sha, "version": 1, "problem": problem, "vector": None if vector is None else at._vector_text(vector)}


class CompareTests(unittest.TestCase):
    def test_all_the_same_person_match(self):
        results = ac.compare_with_each_other({"straight": PERSON, "left": ALSO_PERSON, "up": PERSON}, ORDER)
        self.assertEqual(set(results.values()), {ac.MATCHED})

    def test_the_odd_one_out_is_flagged_even_when_it_is_the_front_view(self):
        results = ac.compare_with_each_other(
            {"straight": SOMEONE_ELSE, "left": PERSON, "right": ALSO_PERSON, "up": PERSON}, ORDER)
        self.assertEqual(results["straight"], ac.MISMATCH)
        self.assertEqual({results[key] for key in ("left", "right", "up")}, {ac.MATCHED})

    def test_a_side_view_counts_through_the_front_view(self):
        left, front, right = unit(1, 1.6), unit(1, 0), unit(1, -1.6)
        self.assertFalse(ac.same_face(left, right))
        results = ac.compare_with_each_other({"left": left, "straight": front, "right": right}, ORDER)
        self.assertEqual(set(results.values()), {ac.MATCHED})

    def test_a_tie_trusts_the_front_view(self):
        results = ac.compare_with_each_other({"left": SOMEONE_ELSE, "straight": PERSON}, ORDER)
        self.assertEqual(results, {"straight": ac.MATCHED, "left": ac.MISMATCH})

    def test_one_image_has_nothing_to_compare_with(self):
        self.assertEqual(ac.compare_with_each_other({"up": PERSON}, ORDER), {"up": ac.NOT_COMPARED})
        self.assertEqual(ac.compare_with_each_other({}, ORDER), {})

    def test_images_without_a_usable_face_get_their_own_result(self):
        results = at.checks_for({
            "straight": entry(PERSON), "left": entry(PERSON),
            "up": entry(problem="No face found in the image"),
            "down": entry(problem="More than one face in the image"),
            "blink": entry(problem="The image can't be read")})
        self.assertEqual(results, {"straight": ac.MATCHED, "left": ac.MATCHED, "up": ac.NO_FACE,
                                   "down": ac.MANY_FACES, "blink": ac.UNREADABLE})


class ReportTests(unittest.TestCase):
    config = HRMConfig("http://h", "k" * 32, False)  # Nothing is sent: the requests are faked.

    def _rows(self, **statuses):
        return [{"employeeCode": "E1", "angle": angle, "verificationStatus": status} for angle, status in statuses.items()]

    def test_only_what_hrm_does_not_have_is_sent(self):
        store = {"E1": {"straight": entry(PERSON, sha="a"), "left": entry(PERSON, sha="b"), "up": entry(SOMEONE_ELSE, sha="c")}}
        with patch.object(at.hrm_sync, "report_angle_checks") as report:
            at._report_checks(self.config, store, self._rows(straight="matched", left=None, up="matched"))
        report.assert_called_once()
        self.assertEqual(sorted(report.call_args.args[2], key=lambda c: c["angle"]),
                         [{"angle": "left", "sha256": "b", "status": "matched"},
                          {"angle": "up", "sha256": "c", "status": "mismatch"}])

    def test_nothing_new_sends_nothing_so_a_report_cannot_cause_another(self):
        store = {"E1": {"straight": entry(PERSON), "left": entry(PERSON)}}
        with patch.object(at.hrm_sync, "report_angle_checks") as report:
            at._report_checks(self.config, store, self._rows(straight="matched", left="matched"))
        report.assert_not_called()

    def test_a_failed_report_does_not_fail_the_sync(self):
        store = {"E1": {"straight": entry(PERSON), "left": entry(PERSON)}}
        with patch.object(at.hrm_sync, "report_angle_checks", side_effect=HRMError("down")), \
                self.assertLogs(level="WARNING"):
            at._report_checks(self.config, store, [])

    def test_the_request_names_the_employee_and_carries_the_results(self):
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b"{}"

        def opener(request, _timeout):
            seen.update(url=request.full_url, method=request.get_method(), body=json.loads(request.data))
            return Response()

        checks = [{"angle": "up", "sha256": "c", "status": "mismatch"}]
        at.hrm_sync.report_angle_checks(self.config, "E 1", checks, opener=opener)
        self.assertEqual(seen["method"], "PUT")
        self.assertTrue(seen["url"].endswith("/employees/E%201/face-angles/verification"), seen["url"])
        self.assertEqual(seen["body"], {"checks": checks})


class GeneratedImageTests(unittest.TestCase):
    """Each generated image is compared with the photo it was made from."""

    def _checker(self, source=b"source"):
        def encode(_backend, content):
            return {b"source": (PERSON, ""), b"same": (ALSO_PERSON, ""), b"other": (SOMEONE_ELSE, ""),
                    b"blank": (None, "No face found in the image"), b"two": (None, "More than one face in the image"),
                    b"nosource": (None, "No face found in the image")}[content]
        patcher = patch.object(at, "encode_angle", side_effect=encode)
        patcher.start()
        self.addCleanup(patcher.stop)
        return at.generated_image_checker(source, backend=object())

    def test_a_generated_image_is_compared_with_its_source(self):
        check = self._checker()
        self.assertEqual(check(b"same"), ac.MATCHED)
        self.assertEqual(check(b"other"), ac.MISMATCH)
        self.assertEqual(check(b"blank"), ac.NO_FACE)
        self.assertEqual(check(b"two"), ac.MANY_FACES)

    def test_a_source_without_a_clear_face_still_checks_the_image_has_one(self):
        check = self._checker(b"nosource")
        self.assertEqual(check(b"same"), ac.NOT_COMPARED)
        self.assertEqual(check(b"blank"), ac.NO_FACE)


if __name__ == "__main__":
    unittest.main()
