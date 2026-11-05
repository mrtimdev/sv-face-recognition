"""Keep the test suite away from any real server."""
from unittest.mock import patch

import pytest

from face_attendance import hrm_servers


@pytest.fixture(autouse=True)
def no_real_hrm():
    # With no saved address the app assumes SV HRM runs on this computer's network address,
    # which on a developer's machine is the real one. Port 9 refuses instead.
    # Automatic would also try the internet address; no test may reach it.
    hrm_servers._chosen.clear()
    with (patch("face_attendance.hrm_sync.DEFAULT_HRM_PORT", 9),
          patch("face_attendance.hrm_servers._open", side_effect=OSError("no network in tests"))):
        yield
