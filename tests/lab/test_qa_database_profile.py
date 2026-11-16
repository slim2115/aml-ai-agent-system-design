"""QA PostgreSQL namespace guards, without opening PostgreSQL or Kafka sockets."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from src.lab import config, database
from src.lab.kafka import analysis_store, store
from src.qa_profile import QAProfile


@pytest.fixture
def profile(tmp_path, monkeypatch):
    value = QAProfile('qa-123456abcdef', root=tmp_path)
    value.base.mkdir(parents=True)
    for module in (config, database, store, analysis_store):
        monkeypatch.setattr(module, 'get_qa_profile', lambda: value)
    return value


def settings(profile=None):
    roles = profile.role_names if profile else database.ROLE_NAMES
    value = {'host': '127.0.0.1', 'port': profile.pg_port if profile else 55432,
             'api_port': profile.api_port if profile else 8765,
             'database': profile.db_name if profile else 'aml_lab', 'api_token': 'f' * 64,
             'roles': {key: {'user': name, 'password': 'a' * 64} for key, name in roles.items()}}
    if profile:
        value.update(run_id=profile.run_id, marker=profile.marker)
    return value


def test_profile_configuration_reads_its_own_file_and_never_default(profile, monkeypatch):
    profile.config_path.write_text(json.dumps(settings(profile)), encoding='utf-8')
    normal_path = profile.root / 'ordinary-connection.json'
    normal_path.write_text('not json', encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', normal_path)
    loaded = config.load_lab_settings()
    assert loaded == settings(profile)
    db = database.LabDatabase(loaded)
    loaded['roles']['api']['user'] = 'aml_api'
    assert db._settings['roles']['api']['user'] == profile.role_names['api']


@pytest.mark.parametrize('field,bad', [
    ('host', 'localhost'), ('host', '198.51.100.1'), ('port', 55432),
    ('database', 'aml_lab'), ('run_id', 'qa-000000000000'), ('marker', database.MARKER),
])
def test_qa_rejects_normal_or_foreign_database_before_connect(profile, monkeypatch, field, bad):
    value = settings(profile)
    value[field] = bad
    monkeypatch.setattr(database.psycopg, 'connect', lambda **_kwargs: pytest.fail('socket opened'))
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database.LabDatabase(value)
    profile.config_path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(RuntimeError, match='LAB_NOT_CONFIGURED'):
        config.load_lab_settings()


@pytest.mark.parametrize('role', ['admin', 'reader', 'student', 'api'])
def test_qa_rejects_every_ordinary_role(profile, role):
    value = settings(profile)
    value['roles'][role]['user'] = database.ROLE_NAMES[role]
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database.LabDatabase(value)


@pytest.mark.parametrize('change', ['api_port', 'missing_api_port', 'missing_marker', 'extra_role'])
def test_qa_file_requires_complete_identity(profile, change):
    value = settings(profile)
    if change == 'api_port':
        value['api_port'] = 8765
    elif change == 'extra_role':
        value['roles']['other'] = deepcopy(value['roles']['reader'])
    else:
        value.pop('api_port' if change == 'missing_api_port' else 'marker')
    profile.config_path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(RuntimeError, match='LAB_NOT_CONFIGURED'):
        config.load_lab_settings()


def test_qa_open_targets_only_its_cluster_database_and_role(profile, monkeypatch):
    calls = []
    monkeypatch.setattr(database.psycopg, 'connect', lambda **kwargs: calls.append(kwargs))
    db = database.LabDatabase(settings(profile))
    for role in profile.role_names:
        db._open(role)
    assert {call['dbname'] for call in calls} == {profile.db_name}
    assert {call['user'] for call in calls} == set(profile.role_names.values())
    assert all(call['host'] == call['hostaddr'] == '127.0.0.1' and
               call['port'] == profile.pg_port for call in calls)
    db._open('admin', maintenance=True)
    assert calls[-1]['dbname'] == 'postgres' and calls[-1]['port'] == profile.pg_port
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        db._open('api', maintenance=True)


def test_profile_cannot_be_switched_under_existing_database(profile, monkeypatch):
    db = database.LabDatabase(settings(profile))
    monkeypatch.setattr(database.psycopg, 'connect', lambda **_kwargs: pytest.fail('socket opened'))
    monkeypatch.setattr(database, 'get_qa_profile', lambda: None)
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        db._open('api')
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database.LabDatabase(settings(profile))


def test_ordinary_profile_retains_fixed_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'get_qa_profile', lambda: None)
    monkeypatch.setattr(database, 'get_qa_profile', lambda: None)
    path = tmp_path / 'default.json'
    path.write_text(json.dumps(settings()), encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', path)
    assert config.load_lab_settings() == settings()
    assert database.LabDatabase(settings())._settings['database'] == 'aml_lab'
    command = 'GRANT CONNECT ON DATABASE aml_lab TO aml_reader, aml_student, aml_api'
    assert database._profile_sql(command) == command


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class MarkerConnection:
    def __init__(self, profile):
        self.profile = profile
        self.commands = []
        self.database = {'name': profile.db_name, 'marker': profile.marker}
        self.schema = {'marker': profile.marker + ':kafka', 'owner': profile.role_names['admin']}
        self.relations = [dict(name=name, kind='r', owner=profile.role_names['admin'])
                          for name in store.TABLES]

    def execute(self, command, params=()):
        command = command if isinstance(command, str) else command.as_string()
        self.commands.append((command, params))
        if command.startswith('SELECT current_database()'):
            return Result([self.database])
        if 'pg_get_userbyid(nspowner)' in command:
            return Result([self.schema])
        if 'FROM pg_namespace WHERE nspname = %s' in command:
            return Result([{'marker': self.profile.marker}])
        if command.startswith('SELECT c.relname'):
            return Result(self.relations)
        return Result()


@pytest.mark.parametrize('scope', ['database_name', 'database_marker', 'schema_marker', 'schema_owner', 'table_owner'])
def test_qa_ownership_guards_reject_ordinary_and_foreign_objects(profile, scope):
    conn = MarkerConnection(profile)
    database._assert_marker(conn)
    database._assert_schema_marker(conn, 'reference')
    store._assert_owned(conn)
    if scope == 'database_name':
        conn.database['name'] = 'aml_lab'
    elif scope == 'database_marker':
        conn.database['marker'] = database.MARKER
    elif scope == 'schema_marker':
        conn.schema['marker'] = store.SCHEMA_MARKER
    elif scope == 'schema_owner':
        conn.schema['owner'] = 'aml_lab_admin'
    else:
        conn.relations[0]['owner'] = 'aml_lab_admin'
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database._assert_marker(conn)
        store._assert_owned(conn)


def test_qa_analysis_relations_require_specific_run_and_owner(profile):
    rows = [dict(name=name, kind='r', owner=profile.role_names['admin'],
                 marker=profile.marker + ':analysis') for name in analysis_store.TABLES]
    analysis_store._check_relations(rows)
    rows[0]['marker'] = analysis_store.ANALYSIS_MARKER
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        analysis_store._check_relations(rows)


def test_qa_ddl_and_grants_contain_only_quoted_qa_identifiers(profile):
    conn = MarkerConnection(profile)
    database._grants(conn, 'practice')
    store._grants(conn)
    rendered = '\n'.join(command for command, _ in conn.commands)
    ddl = store._schema_ddl()
    for default_role in database.ROLE_NAMES.values():
        assert default_role not in rendered and default_role not in ddl
    for role in ('reader', 'student', 'api'):
        assert '"' + profile.role_names[role] + '"' in rendered
    assert 'AUTHORIZATION "' + profile.role_names['admin'] + '"' in ddl
    assert profile.marker + ':kafka' in ddl and store.SCHEMA_MARKER not in ddl
    analysis = analysis_store._analysis_ddl()
    assert analysis.count(profile.marker + ':analysis') == 2
    assert "CHECK (topic = '" + profile.analysis_topic + "')" in analysis
    assert 'aml.lab.analysis-requested.v1' not in analysis
    assert 'analysis_requests' in analysis and 'analysis_deliveries' in analysis


def test_qa_seed_reads_verified_copy_and_never_falls_back(profile):
    shutil.copytree(database.ROOT / 'data/demo-v1', profile.seed_dir)
    rows = database.verified_seed()
    assert len(rows['clients']) == 5 and len(rows['transactions']) == 15
    source = profile.seed_dir / 'clients.json'
    source.write_bytes(source.read_bytes() + b' ')
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database.verified_seed()
    source.unlink()
    with pytest.raises(FileNotFoundError):
        database.verified_seed()


def test_qa_unverified_seed_stops_bootstrap_before_any_connection(profile, monkeypatch):
    profile.seed_dir.mkdir(parents=True)
    (profile.seed_dir / 'manifest.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(database.psycopg, 'connect', lambda **_kwargs: pytest.fail('socket opened'))
    with pytest.raises(database.LabDatabaseError, match='INVALID_STATE'):
        database.bootstrap(settings(profile))
