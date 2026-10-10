"""API key lookup. Keys are never stored in project files, the database or logs.

Order: environment variable, then the OS credential store via ``keyring``
(Windows Credential Manager on Windows).
"""

from __future__ import annotations

import os

from ai_video_studio import APP_ID
from ai_video_studio.core.errors import StudioError

ENV_VARS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "openai": ("OPENAI_API_KEY",)}


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring):
        return None
    return keyring


def secure_store_available() -> bool:
    return _keyring() is not None


def get_api_key(provider: str) -> str | None:
    for var in ENV_VARS.get(provider, ()):
        if os.environ.get(var):
            return os.environ[var].strip()
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(APP_ID, provider) or None
    except Exception:  # noqa: BLE001 - a broken credential store means "no key"
        return None


def key_source(provider: str) -> str | None:
    for var in ENV_VARS.get(provider, ()):
        if os.environ.get(var):
            return f"environment variable {var}"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(APP_ID, provider):
                return "Windows Credential Manager" if os.name == "nt" else "system credential store"
        except Exception:  # noqa: BLE001
            return None
    return None


def set_api_key(provider: str, key: str) -> None:
    key = key.strip()
    if not key:
        raise StudioError("The API key is empty.")
    kr = _keyring()
    if kr is None:
        names = " or ".join(ENV_VARS.get(provider, ()))
        raise StudioError(f"No secure credential store is available on this system. Set the {names} "
                          "environment variable instead.")
    kr.set_password(APP_ID, provider, key)


def delete_api_key(provider: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(APP_ID, provider)
    except Exception:  # noqa: BLE001 - nothing stored
        pass
