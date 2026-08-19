"""Shared test setup.

`get_fundamentals` retries Yahoo rate limits with real sleeps and remembers a
tripped limiter in module state. Neither belongs in a test run: sleeps are
stubbed out (tests that care record them by patching `_sleep` again) and the
breaker starts every test re-armed.
"""
import pytest

from src import fundamentals


@pytest.fixture(autouse=True)
def _calm_fundamentals(monkeypatch):
    monkeypatch.setattr(fundamentals, "_sleep", lambda seconds: None)
    monkeypatch.setattr(fundamentals, "_limiter_exhausted", False)
