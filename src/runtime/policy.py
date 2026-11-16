"""An explicit synthetic replay policy, never a business-policy approval."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from src.config import PROJECT_ROOT, resolve_project_path
from src.contracts.v1 import AnalysisWindow, Digest, Identifier, StrictModel


class PolicyFailure(ValueError):
    code = 'POLICY_NOT_APPROVED'

    def __init__(self):
        super().__init__('An explicit valid synthetic replay policy is required.')


def strict_json(data: bytes):
    """Reject duplicate keys and non-JSON numbers before model validation."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def reject_number(_):
        raise ValueError('JSON floating-point numbers are not accepted in source data')

    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                      parse_float=reject_number, parse_constant=reject_number)


class DemoPolicy(StrictModel):
    schema_version: Literal['1.0']
    policy_id: Identifier
    purpose: Literal['synthetic_learning_only']
    expert_approved: Literal[False]
    assumed_naive_timezone: Literal['UTC']
    snapshot_id: Identifier
    snapshot_manifest_sha256: Digest
    window: AnalysisWindow
    max_snapshot_age_seconds: Annotated[int, Field(ge=0, le=86400)]
    currency_scales: dict[str, Annotated[int, Field(ge=0, le=12)]]
    applicability: dict[str, list[Identifier]]

    @model_validator(mode='after')
    def explicit_demo_parameters(self):
        import re
        if not self.currency_scales or any(re.fullmatch('[A-Z]{3}', c) is None for c in self.currency_scales):
            raise ValueError('explicit currency scales are required')
        if not self.applicability or any(re.fullmatch('[a-z][a-z0-9_]{0,63}', a) is None for a in self.applicability):
            raise ValueError('explicit synthetic alert mapping is required')
        for rule_ids in self.applicability.values():
            if len(rule_ids) != len(set(rule_ids)):
                raise ValueError('duplicate rule mapping')
        return self


def load_demo_policy(path: Path) -> DemoPolicy:
    """No default policy or implicit production authorization is available."""
    try:
        resolved = Path(resolve_project_path(path, root=PROJECT_ROOT, key='demo policy'))
        if resolved.is_symlink() or resolved.stat().st_size > 131072:
            raise ValueError
        return DemoPolicy.model_validate(strict_json(resolved.read_bytes()))
    except (OSError, ValueError, TypeError, ValidationError):
        raise PolicyFailure() from None
