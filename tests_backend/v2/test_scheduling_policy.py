from __future__ import annotations

import json

import pytest
from sqlalchemy import update

from src.backend_v2.storage.schema import platform_config
from src.backend_v2.scheduling_policy import (
    DEFAULT_SCHEDULING_POLICY,
    SchedulingPolicyRepository,
    validate_scheduling_policy,
)
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.lifecycle import initialize_database


def test_scheduling_policy_uses_the_small_machine_defaults_and_closed_schema(
    tmp_path,
) -> None:
    data_root = tmp_path / "data-v2"
    initialized = initialize_database(data_root)
    engine = create_sqlite_engine(initialized.database_path)
    try:
        repository = SchedulingPolicyRepository(engine)
        assert repository.load() == DEFAULT_SCHEDULING_POLICY
        changed = dict(DEFAULT_SCHEDULING_POLICY)
        changed["pageQuantum"] = 2
        changed["minAvailableMemoryMiB"] = 0
        assert repository.save(changed) == changed
        assert repository.load() == changed
        legacy = {**changed, "maxDeepLearningConcurrency": 8}
        with engine.begin() as connection:
            connection.execute(update(platform_config).values(scheduler_policy_json=json.dumps(legacy)))
        assert repository.load() == {**changed, "maxDeepLearningConcurrency": 4}
    finally:
        engine.dispose()

    with pytest.raises(ValueError, match="字段无效"):
        validate_scheduling_policy({**changed, "extra": True})
    with pytest.raises(ValueError, match="最低可用内存"):
        validate_scheduling_policy({**changed, "minAvailableMemoryMiB": 256})
    with pytest.raises(ValueError, match="深度学习并发数"):
        validate_scheduling_policy({**changed, "maxDeepLearningConcurrency": 5})