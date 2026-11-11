import os
from pathlib import Path
import subprocess
import sys

from face_attendance.catalog_transaction import catalog_transaction
from face_attendance.enrollment import _atomic_write


def test_killed_publisher_is_rolled_back_before_next_reader(tmp_path):
    employees = tmp_path / 'employees.json'
    templates = tmp_path / 'templates.pickle'
    employees.write_bytes(b'original mapping')
    templates.write_bytes(b'original templates')
    code = '''
import os, sys
from pathlib import Path
from face_attendance.catalog_transaction import catalog_transaction
from face_attendance.enrollment import _atomic_write
root = Path(sys.argv[1])
with catalog_transaction(root / 'templates.pickle', root / 'employees.json'):
    _atomic_write(root / 'employees.json', lambda f: f.write(b'new mapping'))
    os._exit(19)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 19
    assert employees.read_bytes() == b'new mapping'
    with catalog_transaction(templates, employees):
        assert employees.read_bytes() == b'original mapping'
        assert templates.read_bytes() == b'original templates'


def test_successful_publication_and_exception_rollback(tmp_path):
    employees, templates = tmp_path / 'employees.json', tmp_path / 'templates.pickle'
    with catalog_transaction(templates, employees):
        _atomic_write(employees, lambda f: f.write(b'one'))
        _atomic_write(templates, lambda f: f.write(b'two'))
    try:
        with catalog_transaction(templates, employees):
            _atomic_write(employees, lambda f: f.write(b'wrong'))
            raise RuntimeError('publication failed')
    except RuntimeError:
        pass
    assert employees.read_bytes() == b'one'
    assert templates.read_bytes() == b'two'
