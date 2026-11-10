"""Credential storage backends: macOS Keychain, Windows profile files, or the fake store."""

from limitpilot import paths
from limitpilot.store import fake


def backend() -> str:
    """Name of the backend in use: ``fake`` (practice mode/tests), ``file`` or ``keychain``."""
    if fake.active():
        return "fake"
    return "file" if paths.IS_WIN else "keychain"
