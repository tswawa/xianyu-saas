"""Small offline checks for the built-in updater's readiness and compatibility gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

if os.name != 'posix':
    print('file update contract: Linux runtime checks skipped on this host')
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import file_update as updater
import platform_update as protocol
import version

def rejects(code, action):
    try:
        action()
    except updater.FileUpdateError as error:
        assert error.code == code, error.code
    else:
        raise AssertionError('expected ' + code)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    store = root / 'store'
    store.mkdir()
    base, candidate = root / 'base', root / 'candidate'
    for tree in (base, candidate):
        for relative in ('backend/requirements.txt', 'worker/requirements.txt'):
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('dependency==1\n')
        (tree / 'backend/version.py').write_text(f'UPDATE_DATA_VERSION = {version.UPDATE_DATA_VERSION}\n')
    with patch.dict(os.environ, SAAS_APP_CODE_DIR=str(store)), patch.object(protocol, 'PROJECT_ROOT', base):
        assert not updater.launcher_fresh()
        heartbeat = {'schema': 1, 'pid': os.getpid(), 'code_root': str(base),
                     'active_version': version.VERSION, 'heartbeat_at': time.time(), 'ready': False}
        (store / 'launcher.json').write_text(json.dumps(heartbeat))
        assert not updater.launcher_fresh(), 'not-ready heartbeat admitted'
        heartbeat['ready'] = True
        (store / 'launcher.json').write_text(json.dumps(heartbeat))
        assert updater.launcher_fresh()
        heartbeat['heartbeat_at'] -= updater.HEARTBEAT_MAX_AGE + 1
        (store / 'launcher.json').write_text(json.dumps(heartbeat))
        assert not updater.launcher_fresh(), 'stale heartbeat admitted'
        updater._verify_compatibility(candidate)
        (candidate / 'backend/requirements.txt').write_text('dependency==2\n')
        rejects('update_dependency_changed', lambda: updater._verify_compatibility(candidate))
        (candidate / 'backend/requirements.txt').write_text('dependency==1\n')
        (candidate / 'backend/version.py').write_text(f'UPDATE_DATA_VERSION = {version.UPDATE_DATA_VERSION + 1}\n')
        rejects('update_data_backward_incompatible', lambda: updater._verify_compatibility(candidate))
        key = store / 'mutable-public-key'
        key.write_text('not reached: mutable keys are rejected before parsing')
        with patch.dict(os.environ, SAAS_UPDATE_PUBLIC_KEY_FILE=str(key)):
            rejects('update_public_key_invalid', updater._key_raw)
print('file update contract: readiness, dependency/data incompatibility and key boundary passed')
