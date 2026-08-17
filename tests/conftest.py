"""Shared fixtures.

Every fixture is hermetic: nothing here reads the real login keychain, the
real preferences plist, or the user's Music folder.
"""

from __future__ import annotations

import os

import pytest

# Set before anything imports Qt. The UI suite runs without a display, and on
# a headless machine Qt otherwise aborts at QApplication construction rather
# than raising something a test can skip on.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from neyta import config
from neyta.core.cache import Cache
from neyta.core.jobs import JobQueue
from neyta.settings import FakeKeyring, MemoryPrefs, SecretStore, Settings


@pytest.fixture
def paths(tmp_path) -> config.Paths:
    return config.Paths.under(tmp_path).ensure()


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def settings(paths, fake_keyring) -> Settings:
    return Settings(
        paths=paths,
        prefs=MemoryPrefs(),
        secrets=SecretStore(backend=fake_keyring),
    )


@pytest.fixture
def cache() -> Cache:
    with Cache(None) as c:  # in-memory
        yield c


@pytest.fixture
def disk_cache(paths) -> Cache:
    with Cache(paths.cache_db) as c:
        yield c


@pytest.fixture
def queue() -> JobQueue:
    q = JobQueue(workers=4)
    q.retry_delay = 0.0
    try:
        yield q
    finally:
        q.shutdown(wait=True)
