"""Fixed loopback configuration for this machine's independent teaching stand."""
import json
from pathlib import Path
import re

from src.qa_profile import checked_path, get_qa_profile

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / 'var/postgres/lab-connection.json'


def load_lab_settings():
    try:
        profile = get_qa_profile()
        path = profile.config_path if profile else CONFIG_PATH
        if profile:
            checked_path(path, profile.root)
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        port = profile.pg_port if profile else 55432
        database = profile.db_name if profile else 'aml_lab'
        api_port = profile.api_port if profile else 8765
        if (value['host'] != '127.0.0.1' or type(value['port']) is not int
                or value['port'] != port or value['database'] != database
                or value.get('api_port', None if profile else api_port) != api_port):
            raise ValueError
        if profile and (value.get('run_id') != profile.run_id or value.get('marker') != profile.marker):
            raise ValueError
        expected = (profile.role_names if profile else
                    {'admin': 'aml_lab_admin', 'reader': 'aml_reader', 'student': 'aml_student', 'api': 'aml_api'})
        if set(value['roles']) != set(expected):
            raise ValueError
        for role, user in expected.items():
            record = value['roles'][role]
            if record['user'] != user or re.fullmatch(r'[0-9a-f]{64}', record['password']) is None:
                raise ValueError
        if re.fullmatch(r'[0-9a-f]{64}', value['api_token']) is None:
            raise ValueError
        return value
    except (OSError, KeyError, TypeError, ValueError):
        raise RuntimeError('LAB_NOT_CONFIGURED') from None
