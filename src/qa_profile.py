"""Explicit, owned local QA namespace. No service or filesystem mutation on import.

Defaults remain the teaching stand. QA is opt-in via AML_QA_RUN_ID; no caller
URL, port, database name or directory is accepted from the environment.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
RUN_PATTERN = r'qa-[0-9a-f]{12}'


def checked_path(path: Path, root: Path = ROOT) -> Path:
    path, root = Path(path).absolute(), Path(root).absolute()
    if '..' in path.parts or not path.is_relative_to(root):
        raise ValueError('INVALID_QA_PATH')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('INVALID_QA_PATH')
        if part == root:
            break
    if path.resolve() != path or (path.is_file() and path.stat().st_nlink != 1):
        raise ValueError('INVALID_QA_PATH')
    return path


@dataclass(frozen=True)
class QAProfile:
    run_id: str
    root: Path = ROOT

    def __post_init__(self):
        if not isinstance(self.run_id, str) or re.fullmatch(RUN_PATTERN, self.run_id) is None:
            raise ValueError('INVALID_QA_RUN_ID')
        if not self.root.is_absolute():
            raise ValueError('INVALID_QA_ROOT')

    @property
    def suffix(self): return self.run_id[3:]
    @property
    def base(self): return self.root / 'var/qa/runs' / self.run_id
    @property
    def workflow_dir(self): return self.root / 'var/workflow/qa' / self.run_id
    @property
    def workflow_path(self): return self.workflow_dir / 'api-workflow.sqlite3'
    @property
    def config_path(self): return self.base / 'lab-connection.json'
    @property
    def seed_dir(self): return self.base / 'data/demo-v1'
    @property
    def policy_path(self): return self.base / 'config/demo-policy.json'
    @property
    def export_dir(self): return self.base / 'exports'
    @property
    def chroma_dir(self): return self.base / 'chroma'
    @property
    def marker(self): return 'aml-ai-agent-system-rebuild:qa:' + self.run_id
    @property
    def api_port(self): return 18765
    @property
    def ui_port(self): return 18501
    @property
    def pg_port(self): return 56432
    @property
    def kafka_port(self): return 29092
    @property
    def api_url(self): return f'http://127.0.0.1:{self.api_port}'
    @property
    def db_name(self): return 'aml_qa_' + self.suffix
    @property
    def role_names(self): return {role: f'qa_{self.suffix}_{role}' for role in ('admin', 'reader', 'student', 'api')}
    @property
    def compose_project(self): return 'aml-qa-' + self.suffix
    @property
    def topic_prefix(self): return 'aml.qa.' + self.suffix
    @property
    def topic(self): return self.topic_prefix + '.incident-review.v1'
    @property
    def dlq_topic(self): return self.topic_prefix + '.incident-review.dlq.v1'
    @property
    def group(self): return self.compose_project + '-review-v1'
    @property
    def analysis_topic(self): return self.topic_prefix + '.analysis-requested.v1'
    @property
    def analysis_dlq(self): return self.topic_prefix + '.analysis-requested.dlq.v1'
    @property
    def analysis_group(self): return self.compose_project + '-analysis-v1'

    def owner_record(self):
        return {'schema_version': 1, 'root': str(self.root), 'run_id': self.run_id, 'marker': self.marker}

    def validate(self):
        checked_path(self.base, self.root)
        checked_path(self.workflow_dir, self.root)
        owner = checked_path(self.base / 'owner.json', self.root)
        try:
            if owner.stat().st_size > 4096:
                raise ValueError
            value = json.loads(owner.read_text(encoding='utf-8-sig'))
            if any(value.get(key) != expected for key, expected in self.owner_record().items()):
                raise ValueError
        except (OSError, TypeError, AttributeError, ValueError):
            raise ValueError('QA_OWNER_MISMATCH') from None
        return self


def get_qa_profile():
    run_id = os.environ.get('AML_QA_RUN_ID')
    if run_id is None:
        return None
    return QAProfile(run_id, ROOT).validate()
