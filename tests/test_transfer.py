"""Moving enrolled employees between computers: export, import, conflicts, safety."""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from face_attendance import transfer
from face_attendance.catalog import load_employee_map
from face_attendance.enrollment import EnrollmentService, employee_rows, read_encodings
from face_attendance.enrollment_photos import portrait_name, portrait_path, sample_photo_map
from face_attendance.template_store import read_template_bundle
from face_attendance.transfer import TransferError, export_employees, import_employees
from tests.test_enrollment_photos import PhotoBackend, frame


class Computer:
    """One installation's enrollment files in a temp folder."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog = self.root / "encodings_sface.pickle"
        self.mapping = self.root / "employees.json"

    def enroll(self, label, employee_id, *values):
        service = EnrollmentService(self.catalog, self.mapping, backend=PhotoBackend(), check_quality=False)
        outcome = service.enroll_many([frame(value) for value in values], label, employee_id)
        assert outcome.ok, outcome.message

    def rows(self):
        return {row["name"]: (row["employee_id"], row["employee_name"], row["samples"])
                for row in employee_rows(self.catalog, self.mapping)}

    def photos(self, label):
        return [path for path in sample_photo_map(self.catalog).get(label, []) if path and path.is_file()]


class TransferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mac, self.pc = Computer(self.root / "mac"), Computer(self.root / "pc")
        self.file = self.root / "employees.zip"

    def test_everything_moves_to_an_empty_computer(self):
        self.mac.enroll("Tim Dev", "EMP002", 60, 140)
        self.mac.enroll("Pisey", "EMP004", 90)
        portrait = self.mac.root / "enrollment_photos" / portrait_name("Pisey")
        portrait.write_bytes(self.mac.photos("Pisey")[0].read_bytes())
        self.assertEqual(export_employees(self.file, self.mac.catalog, self.mac.mapping), (2, 3))

        report = import_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertEqual((report.added, report.samples, report.skipped), (["Tim Dev", "Pisey"], 3, []))
        self.assertEqual(self.pc.rows(), self.mac.rows())
        for label in ("Tim Dev", "Pisey"):
            np.testing.assert_array_equal(np.asarray(read_encodings(self.pc.catalog)[label]),
                                          np.asarray(read_encodings(self.mac.catalog)[label]))
            self.assertEqual([p.read_bytes() for p in self.pc.photos(label)],
                             [p.read_bytes() for p in self.mac.photos(label)])
        self.assertEqual(portrait_path(self.pc.catalog, "Pisey").read_bytes(), portrait.read_bytes())

        again = import_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertFalse(again.changed)
        self.assertIn("already here", again.summary())

    def test_new_samples_are_added_to_the_same_employee(self):
        self.mac.enroll("Tim Dev", "EMP002", 60, 140)
        self.pc.enroll("Tim Dev", "EMP002", 60)          # same person, one sample already here
        export_employees(self.file, self.mac.catalog, self.mac.mapping)
        report = import_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertEqual((report.updated, report.samples), (["Tim Dev"], 1))
        self.assertEqual(self.pc.rows()["Tim Dev"][2], 2, "the duplicate sample is not added twice")

    def test_an_id_that_belongs_to_someone_else_is_never_merged(self):
        self.mac.enroll("B Sothea", "EMP001", 60)
        self.mac.enroll("Tim Dev", "EMP002", 90)
        self.mac.enroll("Dara", "EMP009", 120)
        self.pc.enroll("Tim Dev", "EMP001", 140)          # here EMP001 is Tim Dev
        export_employees(self.file, self.mac.catalog, self.mac.mapping)
        report = import_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertEqual(report.added, ["Dara"])
        self.assertIn("B Sothea: ID EMP001 already belongs to Tim Dev", report.skipped)
        self.assertIn("Tim Dev: already enrolled here with ID EMP001", report.skipped)
        self.assertEqual(self.pc.rows()["Tim Dev"], ("EMP001", "Tim Dev", 1))
        self.assertNotIn("B Sothea", self.pc.rows())
        self.assertIn("Skipped", report.summary())

    def test_unsafe_or_foreign_files_are_refused(self):
        not_zip = self.root / "notes.zip"
        not_zip.write_text("hello")
        cases = {"not a zip": not_zip}
        for name, manifest in (("other app", {"format": "other"}),
                               ("newer format", {"format": transfer.FORMAT, "version": 99}),
                               ("other model", {"format": transfer.FORMAT, "version": 1, "model": "dlib",
                                                "employees": []})):
            path = self.root / f"{name}.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(transfer.MANIFEST, json.dumps(manifest))
            cases[name] = path
        for name, path in cases.items():
            with self.subTest(name), self.assertRaises(TransferError):
                import_employees(path, self.pc.catalog, self.pc.mapping)
        self.assertFalse(self.pc.catalog.exists())

    def test_bad_vectors_and_escaping_photo_names_are_ignored(self):
        good = [0.5] * 128
        manifest = {"format": transfer.FORMAT, "version": 1, "model": transfer.TEMPLATE_MODEL,
                    "employees": [
                        {"label": "Dara", "employee_id": "EMP9", "samples": [
                            {"vector": [1.0] * 127, "photo": None},              # wrong length
                            {"vector": ["x"] * 128, "photo": None},              # not numbers
                            {"vector": [float("nan")] * 128, "photo": None},     # not finite
                            {"vector": good, "photo": "photos/../../../evil.jpg"}]},
                        {"label": "", "samples": [{"vector": good}]},
                        {"label": "Empty", "samples": []}]}
        with zipfile.ZipFile(self.file, "w") as archive:
            archive.writestr(transfer.MANIFEST, json.dumps(manifest))
            archive.writestr("photos/../../../evil.jpg", b"not even an image")
        report = import_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertEqual((report.added, report.samples), (["Dara"], 1))
        self.assertEqual(sorted(report.skipped), ["Empty: no usable face samples", "an entry without a name"])
        self.assertEqual(self.pc.photos("Dara"), [])
        self.assertFalse(any(path.name == "evil.jpg" for path in self.root.rglob("*")))

    def test_oversized_entries_are_refused(self):
        self.mac.enroll("Tim Dev", "EMP002", 60)
        export_employees(self.file, self.mac.catalog, self.mac.mapping)
        with patch.object(transfer, "MAX_FILE_BYTES", 10):
            with self.assertRaises(TransferError):
                import_employees(self.file, self.pc.catalog, self.pc.mapping)

    def test_a_failed_import_leaves_nothing_behind(self):
        self.mac.enroll("Tim Dev", "EMP002", 60, 140)
        self.pc.enroll("Dara", "EMP009", 90)
        export_employees(self.file, self.mac.catalog, self.mac.mapping)
        before = {path: path.read_bytes() for path in self.pc.root.rglob("*") if path.is_file()}
        with patch.object(transfer, "write_encodings", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                import_employees(self.file, self.pc.catalog, self.pc.mapping)
        after = {path: path.read_bytes() for path in self.pc.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_nothing_to_export(self):
        with self.assertRaises(TransferError):
            export_employees(self.file, self.pc.catalog, self.pc.mapping)
        self.assertFalse(self.file.exists())

    def test_the_export_holds_no_pickle(self):
        self.mac.enroll("Tim Dev", "EMP002", 60)
        export_employees(self.file, self.mac.catalog, self.mac.mapping)
        with zipfile.ZipFile(self.file) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read(transfer.MANIFEST))
        self.assertTrue(all(name == transfer.MANIFEST or name.endswith(".jpg") for name in names))
        self.assertEqual(manifest["employees"][0]["employee_id"], "EMP002")
        self.assertEqual(len(manifest["employees"][0]["samples"][0]["vector"]), 128)


class EmployeesScreenTransferTests(unittest.TestCase):
    def test_export_then_import_through_the_employees_screen(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from tests.test_dashboard import PreviewCapture, StubEngine, _app, temp_settings
        from face_attendance.dashboard.screens.employees import EmployeesScreen
        _app()

        class Engine(StubEngine):
            reloads = 0

            def reload_catalog(self):
                self.reloads += 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screens = {}
            for name in ("mac", "pc"):
                (root / name).mkdir()
                screens[name] = EmployeesScreen(Engine(), temp_settings(root / name),
                                                preview_capture_factory=lambda: PreviewCapture(None))
                self.addCleanup(screens[name].close)
            mac = Computer(root / "mac")
            mac.catalog = Path(screens["mac"].settings.encodings_path)
            mac.mapping = Path(screens["mac"].settings.employees_path)
            mac.enroll("Dara", "EMP9", 60)
            file = root / "SV-Face-ID-employees"
            with patch("face_attendance.dashboard.screens.employees.QFileDialog.getSaveFileName",
                       return_value=(str(file), "")):
                screens["mac"]._export_employees()
            self.assertTrue(file.with_suffix(".zip").is_file(), ".zip is added to the chosen name")
            with patch("face_attendance.dashboard.screens.employees.QFileDialog.getOpenFileName",
                       return_value=(str(file.with_suffix(".zip")), "")):
                screens["pc"]._import_employees()
            pc = screens["pc"].settings
            self.assertIn("Dara", read_encodings(pc.encodings_path))
            self.assertEqual(load_employee_map(pc.employees_path)["Dara"].employee_id, "EMP9")
            self.assertEqual(screens["pc"].engine.reloads, 1)


if __name__ == "__main__":
    unittest.main()
