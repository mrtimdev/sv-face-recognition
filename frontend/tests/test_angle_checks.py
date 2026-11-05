"""Do an employee's angle photos show the same face, and does HRM hear about it?"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance import angle_checks as ac
from face_attendance import angle_templates as at
from face_attendance.catalog import FaceCatalog
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

    def test_a_lone_angle_keeps_its_result_because_the_sync_has_nothing_to_compare_it_with(self):
        store = {"E1": {"left": entry(PERSON, sha="b")}}
        with patch.object(at.hrm_sync, "report_angle_checks") as report:
            at._report_checks(self.config, store, self._rows(left="matched"))
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
        self.assertEqual(check(b"other"), ac.PROFILE_MISMATCH)
        self.assertEqual(check(b"blank"), ac.NO_FACE)
        self.assertEqual(check(b"two"), ac.MANY_FACES)

    def test_a_source_without_a_clear_face_still_checks_the_image_has_one(self):
        check = self._checker(b"nosource")
        self.assertEqual(check(b"same"), ac.NOT_COMPARED)
        self.assertEqual(check(b"blank"), ac.NO_FACE)

    def test_the_source_problem_is_known_before_anything_is_generated(self):
        self.assertEqual(self._checker().source_problem(), "")
        self.assertEqual(self._checker(b"nosource").source_problem(), ac.NO_FACE)


class ProfileMatchTests(unittest.TestCase):
    def test_angles_must_link_to_the_profile_photo(self):
        results = ac.compare_with_profile({"straight": ALSO_PERSON, "left": SOMEONE_ELSE}, PERSON)
        self.assertEqual(results, {"straight": ac.MATCHED, "left": ac.PROFILE_MISMATCH})

    def test_a_side_view_counts_through_the_front_view(self):
        left, front = unit(1, 1.6), unit(1, 0)
        profile = unit(1, -1.6)
        self.assertFalse(ac.same_face(left, profile))
        self.assertEqual(set(ac.compare_with_profile({"left": left, "straight": front}, profile).values()), {ac.MATCHED})

    def test_angles_that_only_match_each_other_still_fail(self):
        results = ac.compare_with_profile({"left": SOMEONE_ELSE, "right": SOMEONE_ELSE}, PERSON)
        self.assertEqual(set(results.values()), {ac.PROFILE_MISMATCH})

    def test_the_sync_cannot_overturn_a_finding_about_the_profile_photo(self):
        store = {"E1": {"straight": entry(PERSON), "left": entry(PERSON), "up": entry(problem="No face found in the image")}}
        rows = [{"employeeCode": "E1", "angle": angle, "verificationStatus": ac.PROFILE_MISMATCH}
                for angle in ("straight", "left", "up")]
        with patch.object(at.hrm_sync, "report_angle_checks") as report:
            at._report_checks(ReportTests.config, store, rows)
        self.assertEqual([check["angle"] for check in report.call_args.args[2]], ["up"])


class SavedAngleTests(unittest.TestCase):
    config = HRMConfig("http://h", "k" * 32, False)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        faces = {b"person": (PERSON, ""), b"also": (ALSO_PERSON, ""), b"other": (SOMEONE_ELSE, ""),
                 b"blank": (None, "No face found in the image")}
        patcher = patch.object(at, "encode_angle", side_effect=lambda _backend, content: faces[content])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _items(self, **contents):
        items = []
        for key, (content, check) in contents.items():
            path = self.root / f"{key}.jpg"
            path.write_bytes(content)
            items.append({"key": key, "path": path, "sha256": key + "-sha", "check": check})
        return items

    def _verify(self, items, profile):
        with patch.object(at.hrm_sync, "report_angle_checks") as report:
            at.verify_saved_angles(self.config, "E1", items, profile, backend=object())
        return {item["key"]: item["check"] for item in items}, report

    def test_each_angle_is_compared_with_the_profile_photo_and_hrm_hears_only_what_changed(self):
        items = self._items(straight=(b"also", "matched"), left=(b"other", "matched"), up=(b"blank", None))
        results, report = self._verify(items, b"person")
        self.assertEqual(results, {"straight": ac.MATCHED, "left": ac.PROFILE_MISMATCH, "up": ac.NO_FACE})
        self.assertEqual(report.call_args.args[2],
                         [{"angle": "left", "sha256": "left-sha", "status": ac.PROFILE_MISMATCH},
                          {"angle": "up", "sha256": "up-sha", "status": ac.NO_FACE}])

    def test_nothing_new_reports_nothing(self):
        _results, report = self._verify(self._items(straight=(b"also", "matched")), b"person")
        report.assert_not_called()

    def test_without_a_usable_profile_photo_the_angles_are_compared_with_each_other(self):
        items = self._items(straight=(b"person", None), left=(b"also", None), up=(b"person", None), down=(b"other", None))
        results, _report = self._verify(items, b"blank")
        self.assertEqual(results["down"], ac.MISMATCH)
        self.assertEqual(results["straight"], ac.MATCHED)
        results, _report = self._verify(self._items(straight=(b"person", None), down=(b"other", None)), None)
        self.assertEqual(results["down"], ac.MISMATCH)

    def test_a_failed_report_keeps_the_results(self):
        items = self._items(left=(b"other", "matched"))
        with patch.object(at.hrm_sync, "report_angle_checks", side_effect=HRMError("down")), \
                self.assertLogs(level="WARNING"):
            at.verify_saved_angles(self.config, "E1", items, b"person", backend=object())
        self.assertEqual(items[0]["check"], ac.PROFILE_MISMATCH)


class ScoreTests(unittest.TestCase):
    def test_percentages_cover_all_six_required_angles(self):
        result = ac.score([ac.MATCHED] * 5 + [ac.PROFILE_MISMATCH])
        self.assertEqual((result.verified_percent, result.warning_percent), (83, 17))
        self.assertTrue(result.recognizable)
        result = ac.score([ac.MATCHED] * 6)
        self.assertEqual((result.verified_percent, result.warning_percent), (100, 0))
        self.assertEqual(ac.score([]).verified_percent, 0)

    def test_missing_and_unchecked_angles_count_as_neither(self):
        result = ac.score([ac.MATCHED, ac.NOT_COMPARED, None])
        self.assertEqual((result.verified, result.warnings, result.verified_percent), (1, 0, 17))

    def test_verified_must_be_higher_than_warning(self):
        self.assertFalse(ac.score([ac.MATCHED] * 3 + [ac.MISMATCH] * 3).recognizable)
        self.assertFalse(ac.score([ac.MATCHED] * 2 + [ac.NO_FACE] * 4).recognizable)
        self.assertFalse(ac.score([]).recognizable)
        self.assertTrue(ac.score([ac.MATCHED] * 4 + [ac.MISMATCH] * 2).recognizable)


class LiveMonitorTests(unittest.TestCase):
    """EMP0150 once held EMP-00144's photo as its "down" angle, so EMP-00144 could be taken for EMP0150."""

    def _angles(self, down):
        return {"straight": entry(PERSON, sha="a"), "blink": entry(ALSO_PERSON, sha="b"), "left": entry(PERSON, sha="c"),
                "right": entry(ALSO_PERSON, sha="d"), "up": entry(PERSON, sha="e"), "down": down}

    def _ids(self, vectors):
        return {vector.tobytes() for vector in vectors}

    def test_only_verified_angles_are_matched(self):
        vectors = at.recognition_vectors(self._angles(entry(SOMEONE_ELSE, sha="f")))
        self.assertEqual(len(vectors), 5)
        self.assertNotIn(SOMEONE_ELSE.astype("<f8").tobytes(), self._ids(vectors))

    def test_a_finding_about_the_profile_photo_excludes_an_angle_the_others_agree_with(self):
        angles = self._angles({**entry(ALSO_PERSON, sha="f"), "check": ac.PROFILE_MISMATCH})
        self.assertEqual(len(at.recognition_vectors(angles)), 5)
        self.assertEqual(at.final_checks(angles)["down"], ac.PROFILE_MISMATCH)

    def test_more_warnings_than_verified_means_nobody_is_matched(self):
        flagged = {"check": ac.PROFILE_MISMATCH}
        angles = {key: {**entry(PERSON, sha=key), **flagged} for key in ("straight", "blink", "left", "right")}
        angles.update(up=entry(PERSON, sha="e"), down=entry(PERSON, sha="f"))
        self.assertEqual(at.recognition_vectors(angles), [])

    def test_an_even_split_means_nobody_is_matched(self):
        angles = {key: {**entry(PERSON, sha=key), "check": ac.PROFILE_MISMATCH} for key in ("straight", "blink", "left")}
        angles.update(right=entry(PERSON, sha="d"), up=entry(PERSON, sha="e"), down=entry(PERSON, sha="f"))
        self.assertEqual(at.recognition_vectors(angles), [])

    def test_a_lone_unverified_angle_is_not_matched_but_a_verified_one_is(self):
        self.assertEqual(at.recognition_vectors({"up": entry(PERSON)}), [])
        self.assertEqual(len(at.recognition_vectors({"up": {**entry(PERSON), "check": ac.MATCHED}})), 1)

    def test_the_catalog_gives_the_flagged_face_to_nobody_but_its_owner(self):
        store = {"EMP0150": self._angles(entry(SOMEONE_ELSE, sha="f")),
                 "EMP-00144": {key: entry(SOMEONE_ELSE if key != "up" else unit(0.1, 1), sha=key)
                               for key in ("straight", "blink", "left", "right", "up", "down")}}
        catalog = FaceCatalog("unused", "unused", resolve=lambda employee: employee, angle_store=store)
        owners = {vector.tobytes(): employee.employee_id for employee, vector in zip(catalog.employees, catalog.encodings)
                  if employee.employee_id == "EMP0150"}
        self.assertEqual(len(owners), 2)  # PERSON and ALSO_PERSON, five angles between them.
        self.assertNotIn(SOMEONE_ELSE.tobytes(), owners)
        self.assertEqual(sum(1 for employee in catalog.employees if employee.employee_id == "EMP0150"), 5)
        self.assertEqual(sum(1 for employee in catalog.employees if employee.employee_id == "EMP-00144"), 6)

    def test_counts_and_problems_say_what_the_monitor_does(self):
        store = {"E1": self._angles(entry(SOMEONE_ELSE, sha="f"))}
        self.assertEqual(at.counts(store), {"e1": 5})
        self.assertIn("different person", at.problems_for(store, "E1")["down"])

    def test_a_sync_stores_hrms_result_without_downloading_the_image_again(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.json"
            at._save(path, {"E1": {"up": entry(PERSON, sha="a")}})
            rows = [{"employeeCode": "E1", "angle": "up", "sha256": "a", "version": 1,
                     "verificationStatus": ac.PROFILE_MISMATCH}]
            with patch.object(at.hrm_sync, "angle_summary", return_value=rows), \
                    patch.object(at.hrm_sync, "angle_image") as download, \
                    patch.object(at.hrm_sync, "report_angle_checks"):
                result = at.sync(ReportTests.config, path, backend=object())
            download.assert_not_called()
            self.assertTrue(result.changed)
            self.assertEqual(at.load(path)["E1"]["up"]["check"], ac.PROFILE_MISMATCH)


class NewProfilePhotoTests(unittest.TestCase):
    def _check(self, new, current):
        faces = {b"person": (PERSON, ""), b"also": (ALSO_PERSON, ""), b"other": (SOMEONE_ELSE, ""),
                 b"blank": (None, "No face found in the image"), b"two": (None, "More than one face in the image")}
        with patch.object(at, "encode_angle", side_effect=lambda _backend, content: faces[content]):
            return at.check_new_profile_photo(new, current, backend=object())

    def test_results(self):
        self.assertEqual(self._check(b"also", b"person"), ac.MATCHED)
        self.assertEqual(self._check(b"other", b"person"), ac.MISMATCH)
        self.assertEqual(self._check(b"blank", b"person"), ac.NO_FACE)
        self.assertEqual(self._check(b"two", b"person"), ac.MANY_FACES)

    def test_nothing_to_compare_with_is_not_a_warning(self):
        self.assertEqual(self._check(b"person", None), ac.NOT_COMPARED)
        self.assertEqual(self._check(b"person", b"blank"), ac.NOT_COMPARED)


if __name__ == "__main__":
    unittest.main()
