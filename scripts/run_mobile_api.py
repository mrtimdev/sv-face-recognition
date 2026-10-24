#!/usr/bin/env python3
"""Start the mobile API with a persistent encryption key and existing accounts."""
import argparse
import getpass
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def lan_address():
    """Best-effort LAN IP for the mobile Server settings hint (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('10.255.255.255', 1))
            return probe.getsockname()[0]
    except OSError:
        return None


def load_or_create_key(parser, key_path):
    """Return the Fernet key, creating it once with owner-only permissions.

    Pending encrypted samples depend on this key, so an unreadable key file is
    reported instead of being silently replaced.
    """
    from cryptography.fernet import Fernet
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = key_path.read_text().strip()
    else:
        key = Fernet.generate_key().decode()
        with os.fdopen(fd,'w') as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        parser.error(f'{key_path} is empty or not a valid Fernet key. Restore it from backup, or delete it '
                     'and cancel unfinished mobile enrollments (their pending samples cannot be decrypted).')
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--ssl-certfile',help='TLS certificate (PEM). Release mobile builds require HTTPS')
    parser.add_argument('--ssl-keyfile',help='TLS private key (PEM) for --ssl-certfile')
    parser.add_argument('--prepare-model',action='store_true',help='Download and verify UniFace head-pose weights, then exit')
    parser.add_argument('--create-admin',action='store_true',help='Create an admin interactively before starting')
    args = parser.parse_args()
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error('--ssl-certfile and --ssl-keyfile must be given together')
    for option in (args.ssl_certfile, args.ssl_keyfile):
        if option and not Path(option).expanduser().is_file():
            parser.error(f'TLS file not found: {option}')

    import numpy as np
    from face_attendance.guided_enrollment import UniFacePoseEstimator

    def prepare_model():
        print('Preparing UniFace head-pose model…')
        try:
            UniFacePoseEstimator().estimate(np.zeros((128,128,3),dtype=np.uint8))
        except RuntimeError as exc:
            parser.error(str(exc))
        print('Head-pose model ready.')

    if args.prepare_model:
        prepare_model()
        return

    import uvicorn
    from face_attendance.mobile_api import load_account_settings
    from face_attendance.users import UserRepository
    # Same accounts database the API itself signs in against.
    repo = UserRepository(load_account_settings())
    if args.create_admin:
        username = input('New admin username: ').strip()
        full_name = input('Full name: ').strip()
        password = getpass.getpass('Password (12+ characters): ')
        confirmation = getpass.getpass('Confirm password: ')
        if not username or len(password) < 12 or password != confirmation:
            parser.error('Username is required; passwords must match and have at least 12 characters')
        if any(user.username == username for user in repo.list_users()):
            parser.error('That username already exists; use the existing account')
        repo.create_user(username,full_name,'','',password,role='admin')
        print('Admin account created.')
    if not any(user.role == 'admin' and not user.disabled and user.has_permission('manage_employees') for user in repo.list_users()):
        parser.error('No active enrollment administrator. Create one in the desktop app or run with --create-admin')
    if not os.getenv('SV_ENROLLMENT_KEY'):
        os.environ['SV_ENROLLMENT_KEY'] = load_or_create_key(parser, ROOT / '.mobile_api.key')
    # Cheap configuration checks run first; only then load the model.
    prepare_model()

    scheme = 'https' if args.ssl_certfile else 'http'
    host = args.host
    if host in ('0.0.0.0', '::', ''):
        host = lan_address() or 'localhost'
    print(f'Mobile API listening on {args.host}:{args.port}.')
    print(f'Enter this in the mobile app Server settings: {scheme}://{host}:{args.port}')
    if scheme == 'http' and args.host not in ('127.0.0.1', 'localhost', '::1'):
        print('WARNING: serving plain HTTP on the network. Passwords, session tokens and face frames are '
              'not encrypted in transit, and release mobile builds refuse HTTP. Use --ssl-certfile/--ssl-keyfile '
              'or a TLS reverse proxy outside development.')
    print('Desktop dashboard picks up completed mobile enrollments automatically.')
    uvicorn.run('face_attendance.mobile_api:create_app',factory=True,host=args.host,port=args.port,workers=1,
                ws_max_size=2_000_000,ws_max_queue=1,
                ssl_certfile=args.ssl_certfile,ssl_keyfile=args.ssl_keyfile)


if __name__ == '__main__':
    main()
