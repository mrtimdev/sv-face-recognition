from types import SimpleNamespace
from unittest.mock import patch

from face_attendance.dashboard import sysmon


def test_unavailable_cpu_frequency_does_not_disable_other_metrics():
    with (patch.object(sysmon.psutil, 'cpu_freq', side_effect=RuntimeError('frequency unavailable'), create=True),
          patch.object(sysmon.psutil, 'cpu_percent', return_value=0),
          patch.object(sysmon.psutil, 'virtual_memory', return_value=SimpleNamespace(used=12, total=32, percent=37.5))):
        snapshot, _, _ = sysmon._take_snapshot(None, None, 0)
    assert snapshot.cpu_freq_mhz == 0
    assert snapshot.ram_used_bytes == 12


def test_hosts_without_a_cpu_frequency_api_still_report_other_metrics():
    with (patch.object(sysmon.psutil, 'cpu_freq', None, create=True),
          patch.object(sysmon.psutil, 'cpu_percent', return_value=0),
          patch.object(sysmon.psutil, 'virtual_memory', return_value=SimpleNamespace(used=12, total=32, percent=37.5))):
        snapshot, _, _ = sysmon._take_snapshot(None, None, 0)
    assert snapshot.cpu_freq_mhz == 0
    assert snapshot.ram_used_bytes == 12


def test_resource_monitor_survives_a_failed_poll_and_retries():
    monitor = sysmon.Monitor(interval=.001)
    received = []
    monitor.snapshotReady.connect(received.append)
    calls = []

    def take_snapshot(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('host telemetry unavailable')
        monitor._stop.set()
        return sysmon.Snapshot(timestamp=1), None, None

    with patch.object(sysmon, '_take_snapshot', side_effect=take_snapshot):
        monitor.run()
    assert len(calls) == 2
    assert len(received) == 1
