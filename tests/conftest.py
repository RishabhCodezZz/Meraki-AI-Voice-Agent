"""Test isolation.

A developer's real .env is loaded at import time by meraki.config, so without
this the env-fallback path would silently satisfy keys the tests expect to be
absent.
"""

import pytest

API_KEY_VARS = ("DEEPGRAM_API_KEY", "OLLAMA_API_KEY", "MURF_API_KEY")


@pytest.fixture(autouse=True)
def _clear_api_keys(monkeypatch):
    for name in API_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
