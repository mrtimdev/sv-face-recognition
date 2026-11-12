"""Launch the desktop dashboard or serve the browser dashboard with --web."""
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--web" in args:
        from face_attendance.web.server import main as launch
        args.remove("--web")
    else:
        from face_attendance.dashboard.app import main as launch
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
