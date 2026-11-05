"""Sending face angles to SV HRM, with no network access."""
import io
import json
import unittest
import urllib.error

from face_attendance import hrm_sync as hrm
from face_attendance.hrm_sync import HRMConfig, HRMError
from face_attendance.settings import Settings

KEY = "face-recognition-key-0123456789abcdef"
CONFIG = HRMConfig("https://hrm.example.com/", KEY)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeHTTP:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout, follow_redirects=False):
        self.requests.append((request, follow_redirects))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(json.dumps(answer).encode())


def http_error(status, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return urllib.error.HTTPError("https://x", status, "error", {}, io.BytesIO(raw))


def header(request, name):
    return dict((k.lower(), v) for k, v in request.header_items()).get(name.lower())


class UploadTests(unittest.TestCase):
    def test_upload_puts_the_image_for_the_employee_with_the_key_in_a_header(self):
        http = FakeHTTP({"success": True, "data": {"angle": "left", "version": 2}})
        result = hrm.upload_angle(CONFIG, "EMP 002/x", "left", b"\xff\xd8jpeg", opener=http)
        request, follow = http.requests[0]
        self.assertEqual(result, {"angle": "left", "version": 2})
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url,
                         "https://hrm.example.com/api/face-recognition/employees/EMP%20002%2Fx/face-angles/left")
        self.assertEqual(header(request, "X-API-KEY"), KEY)
        self.assertNotIn(KEY, request.full_url)
        self.assertFalse(follow)  # A redirect must never carry the key elsewhere.
        self.assertIn(b'name="image"; filename="left.jpg"', request.data)
        self.assertIn(b'name="source"\r\n\r\nAI_GENERATED', request.data)
        self.assertIn(b"\xff\xd8jpeg", request.data)

    def test_upload_reads_a_saved_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "up.jpg"
            path.write_bytes(b"\xff\xd8abc")
            http = FakeHTTP({"data": {}})
            hrm.upload_angle(CONFIG, "EMP002", "up", path, opener=http)
        self.assertIn(b"\xff\xd8abc", http.requests[0][0].data)

    def test_errors_are_plain_and_never_show_the_key(self):
        cases = {401: "rejected the API key", 404: "no such employee", 503: "switched off"}
        for status, text in cases.items():
            http = FakeHTTP(http_error(status, {"message": f"detail {KEY}"}))
            with self.assertRaises(HRMError) as caught:
                hrm.upload_angle(CONFIG, "EMP002", "left", b"x", opener=http)
            self.assertEqual(caught.exception.status, status)
            self.assertIn(text, str(caught.exception))
            self.assertNotIn(KEY, str(caught.exception))

    def test_unreachable_server(self):
        http = FakeHTTP(urllib.error.URLError("refused"))
        with self.assertRaises(HRMError) as caught:
            hrm.upload_angle(CONFIG, "EMP002", "left", b"x", opener=http)
        self.assertIn("hrm.example.com", str(caught.exception))

    def test_needs_an_employee_code_and_an_image(self):
        with self.assertRaises(HRMError):
            hrm.upload_angle(CONFIG, " ", "left", b"x", opener=FakeHTTP())
        with self.assertRaises(HRMError):
            hrm.upload_angle(CONFIG, "EMP002", "left", b"", opener=FakeHTTP())

    def test_check_connection_returns_the_angle_keys(self):
        http = FakeHTTP({"data": [{"angle": "straight"}, {"angle": "blink"}]})
        self.assertEqual(hrm.check_connection(CONFIG, opener=http), ["straight", "blink"])
        self.assertTrue(http.requests[0][0].full_url.endswith("/api/face-recognition/face-angles/angles"))


class ConfigTests(unittest.TestCase):
    def test_problems_in_plain_words(self):
        self.assertIn("server address", hrm.configuration_problem(HRMConfig("", KEY)))
        self.assertIn("API key", hrm.configuration_problem(HRMConfig("https://h", "")))
        self.assertIn("https://", hrm.configuration_problem(HRMConfig("hrm.example.com", KEY)))
        self.assertEqual(hrm.configuration_problem(CONFIG), "")

    def test_settings_round_trip(self):
        settings = Settings()
        self.assertFalse(hrm.config_from_settings(settings).configured)
        settings.update({"hrm_api_url": " https://h.example ", "hrm_api_key": KEY, "hrm_auto_upload": False})
        config = hrm.config_from_settings(settings)
        self.assertEqual(config.base_url, "https://h.example")
        self.assertFalse(config.auto_upload)
        self.assertNotIn(KEY, repr(config))

    def test_plain_http_warning_only_for_public_hosts(self):
        self.assertTrue(hrm.plain_http_warning("http://hrm.example.com"))
        self.assertEqual(hrm.plain_http_warning("http://192.168.1.10:8080"), "")
        self.assertEqual(hrm.plain_http_warning("https://hrm.example.com"), "")


if __name__ == "__main__":
    unittest.main()
