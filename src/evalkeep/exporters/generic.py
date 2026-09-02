"""The portable JSONL export: one approved test per line, runner-independent.

Promptfoo is a choice, not a commitment. This format carries everything a
different runner would need -- input, fixtures, expectations and provenance --
so a suite is never trapped inside one tool's configuration language.
"""

from __future__ import annotations

import json
from typing import Any

from evalkeep.regression import RegressionTest


def to_record(test: RegressionTest) -> dict[str, Any]:
    return {
        "test_id": test.test_id,
        "status": test.status.value,
        "input": test.input.to_dict(),
        "expectations": [expectation.to_dict() for expectation in test.expectations],
        "fixtures": [fixture.to_dict() for fixture in test.fixtures],
        "provenance": test.provenance.to_dict(),
        "reviewer": test.reviewer,
        "reviewed_at": test.reviewed_at.isoformat() if test.reviewed_at else None,
        "edited": test.edited,
    }


def to_jsonl(tests: list[RegressionTest]) -> str:
    return "".join(json.dumps(to_record(test), sort_keys=True) + "\n" for test in tests)
