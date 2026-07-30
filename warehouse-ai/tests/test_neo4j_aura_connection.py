import logging

import pytest
from neo4j import READ_ACCESS

from app.repositories.neo4j import Neo4jConnectivityError, Neo4jRepository


class _Result:
    def single(self):
        return {"result": 1}


class _Session:
    def __init__(self, query_log: list[str], error: Exception | None = None):
        self.query_log = query_log
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def run(self, query: str):
        self.query_log.append(query)
        if self.error is not None:
            raise self.error
        return _Result()


class _Driver:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.session_kwargs = {}
        self.query_log: list[str] = []

    def session(self, **kwargs):
        self.session_kwargs = kwargs
        return _Session(self.query_log, self.error)


def _repository(driver: _Driver) -> Neo4jRepository:
    repository = Neo4jRepository.__new__(Neo4jRepository)
    repository.driver = driver
    repository.database = "neo4j"
    repository.host = "example.databases.neo4j.io"
    repository.uses_tls = True
    return repository


def test_healthcheck_uses_named_read_session_and_safe_query() -> None:
    driver = _Driver()

    result = _repository(driver).healthcheck()

    assert driver.session_kwargs == {
        "database": "neo4j",
        "default_access_mode": READ_ACCESS,
    }
    assert driver.query_log == ["RETURN 1 AS result"]
    assert result["ok"] is True
    assert result["tls"] is True


def test_healthcheck_does_not_expose_driver_error_details(caplog) -> None:
    driver = _Driver(RuntimeError("sensitive-driver-detail"))

    with caplog.at_level(logging.WARNING), pytest.raises(
        Neo4jConnectivityError
    ) as captured:
        _repository(driver).healthcheck()

    assert "sensitive-driver-detail" not in str(captured.value)
    assert "sensitive-driver-detail" not in caplog.text
    assert "RuntimeError" in caplog.text
