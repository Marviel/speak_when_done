"""Shared test fixtures.

Importing speak_when_done.daemon (as the queue tests do at collection time)
registers the warm-synthesis hook `swd._GENERATOR` as an import side effect —
that's the point in the live daemon process, but it must not leak into tests
of the library's cold (subprocess) path. Reset it around every test.
"""

import pytest

import speak_when_done as swd


@pytest.fixture(autouse=True)
def _reset_generator(monkeypatch):
    monkeypatch.setattr(swd, "_GENERATOR", None)
