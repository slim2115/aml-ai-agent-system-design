"""Trusted local simulator identity, not authentication or production IAM.

Only service code constructs Principal. HTTP/GUI request payloads must never
choose subject_id or role. The default binds a stable synthetic subject to the
OS account running the local service. No real banking authority is implied.
"""
from dataclasses import dataclass
import getpass
import hashlib

from src.contracts.v1 import Actor


@dataclass(frozen=True)
class Principal:
    subject_id: str
    role: str = 'operator'

    def __post_init__(self):
        Actor.model_validate({'subject_id': self.subject_id, 'role': self.role})

    def actor(self) -> dict:
        return {'subject_id': self.subject_id, 'role': self.role}


def trusted_local_principal(*, role: str = 'operator') -> Principal:
    """Called once in trusted service setup; role is never read from a request."""
    account = getpass.getuser()
    subject = 'local-' + hashlib.sha256(account.encode('utf-8')).hexdigest()[:24]
    return Principal(subject, role)
