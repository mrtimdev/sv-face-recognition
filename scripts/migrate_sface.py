"""Offline migration: saved enrollment photos -> separate SFace catalog."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from face_attendance.config import ROOT
from face_attendance.migration import migrate_photos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy', type=Path, default=ROOT / 'encodings.pickle')
    parser.add_argument('--output', type=Path, default=ROOT / 'encodings_sface.pickle')
    parser.add_argument('--employees', type=Path, default=ROOT / 'employees.json')
    parser.add_argument('--photos', type=Path)
    args = parser.parse_args()
    report = migrate_photos(args.legacy, args.output, args.employees, args.photos)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
