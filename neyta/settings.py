"""Preferences and the Keychain-backed credential store.

Two stores with one facade:

  prefs    ordinary settings — window state, last format, download folder.
           QSettings on macOS, so it lands in the usual plist.
  secrets  passwords and tokens — macOS Keychain via `keyring`. No secret is
           ever written to disk in plaintext and none goes in the repo.

Which of the two a field uses is declared once in SERVICES, so the settings
dialog, the first-run wizard and the global wipe all agree by construction
rather than by three separate lists that can drift apart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol

from . import config

log = logging.getLogger(__name__)

KEYRING_PREFIX = "NEYTA"


# ---------------------------------------------------------------------------
# Service / credential declarations
# ---------------------------------------------------------------------------

FieldKind = Literal["text", "password", "path", "file"]


@dataclass(frozen=True)
class CredField:
    name: str
    label: str
    kind: FieldKind = "text"
    #: True -> Keychain. False -> prefs (a folder path is not a secret).
    secret: bool = False
    required: bool = False
    help: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    required: bool
    fields: tuple[CredField, ...]
    note: str = ""

    def field(self, name: str) -> CredField:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.key} has no field {name!r}")


SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        key="soulseek",
        label="Soulseek",
        required=True,
        note=(
            "Required for the Soulseek tab. The network refuses clients that "
            "do not share, so the shared folder must point at real music."
        ),
        fields=(
            CredField("username", "Username", "text", secret=False, required=True),
            CredField("password", "Password", "password", secret=True, required=True),
            CredField(
                "share_dir",
                "Folder to share",
                "path",
                secret=False,
                required=True,
                help="Pointing this at an empty folder will get you banned.",
            ),
        ),
    ),
    ServiceSpec(
        key="soundcloud",
        label="SoundCloud",
        required=False,
        note=(
            "Optional. Public tracks need nothing — search and download were "
            "verified with no credentials. A token only matters for private "
            "tracks or your own paid content."
        ),
        fields=(
            CredField("oauth_token", "OAuth token", "password", secret=True),
        ),
    ),
    ServiceSpec(
        key="youtube",
        label="YouTube",
        required=False,
        note=(
            "Optional, off by default. Everything works unauthenticated. A "
            "cookies file only helps with rate limiting and age-gated videos. "
            "Automated access with account cookies runs against YouTube's "
            "terms and carries some account risk."
        ),
        fields=(
            CredField(
                "cookie_file",
                "Cookies file",
                "file",
                secret=False,
                help="A file you export yourself. NEYTA never reads your "
                "browser's cookie store directly.",
            ),
        ),
    ),
    ServiceSpec(
        key="discogs",
        label="Discogs",
        required=False,
        note="Optional. Only raises samplette-local's crawl rate.",
        fields=(CredField("token", "Token", "password", secret=True),),
    ),
    ServiceSpec(
        key="filmot",
        label="Filmot",
        required=False,
        note=(
            "Optional. With a supporter key, phrase search can be switched "
            "from the top-N hybrid to a true global caption index. Paste the "
            "key here, then choose Filmot as the phrase search engine above."
        ),
        fields=(CredField("api_key", "API key", "password", secret=True),),
    ),
    ServiceSpec(
        key="lalal",
        label="LALAL.AI",
        required=False,
        note=(
            "Optional, paid. A licence key lets separation run on their "
            "machines instead of this one. Choose it as the stem engine above "
            "to use it; UVR stays the default and stays free."
        ),
        fields=(CredField("api_key", "Licence key", "password", secret=True),),
    ),
)


def service(key: str) -> ServiceSpec:
    for spec in SERVICES:
        if spec.key == key:
            return spec
    raise KeyError(f"unknown service {key!r}")


# ---------------------------------------------------------------------------
# Preference backends
# ---------------------------------------------------------------------------


class PrefBackend(Protocol):
    """Values are JSON round-tripped, so both backends behave identically and
    QSettings' habit of returning everything as a string never leaks out."""

    def get_raw(self, key: str) -> str | None: ...
    def set_raw(self, key: str, value: str) -> None: ...
    def remove(self, key: str) -> None: ...
    def keys(self) -> list[str]: ...
    def clear(self) -> None: ...


class MemoryPrefs:
    """Backend for tests and headless runs."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(initial or {})

    def get_raw(self, key: str) -> str | None:
        return self._data.get(key)

    def set_raw(self, key: str, value: str) -> None:
        self._data[key] = value

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self) -> list[str]:
        return sorted(self._data)

    def clear(self) -> None:
        self._data.clear()


class JsonPrefs:
    """File-backed backend. Used by the CLI in phase 2, which has no Qt."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except (ValueError, OSError):
                log.warning("unreadable prefs at %s, starting fresh", self.path)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        tmp.replace(self.path)

    def get_raw(self, key: str) -> str | None:
        return self._data.get(key)

    def set_raw(self, key: str, value: str) -> None:
        self._data[key] = value
        self._flush()

    def remove(self, key: str) -> None:
        if self._data.pop(key, None) is not None:
            self._flush()

    def keys(self) -> list[str]:
        return sorted(self._data)

    def clear(self) -> None:
        self._data.clear()
        self._flush()


class QtPrefs:
    """QSettings backend — the real one. Imports Qt lazily so that importing
    neyta.settings in a headless test does not pull in PySide6."""

    def __init__(self, org: str = config.APP_ORG, app: str = config.APP_NAME) -> None:
        from PySide6.QtCore import QSettings

        self._s = QSettings(org, app)

    def get_raw(self, key: str) -> str | None:
        value = self._s.value(key)
        return None if value is None else str(value)

    def set_raw(self, key: str, value: str) -> None:
        self._s.setValue(key, value)
        self._s.sync()

    def remove(self, key: str) -> None:
        self._s.remove(key)
        self._s.sync()

    def keys(self) -> list[str]:
        return sorted(self._s.allKeys())

    def clear(self) -> None:
        self._s.clear()
        self._s.sync()


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------


class SecretStore:
    """Keychain access, one entry per (service, field).

    keyring cannot enumerate what it holds, so `wipe` walks the SERVICES
    declaration instead of a remembered list — nothing can be orphaned by a
    crash between writing a secret and recording that it exists.
    """

    def __init__(self, backend: Any | None = None, prefix: str = KEYRING_PREFIX) -> None:
        self.prefix = prefix
        if backend is None:
            import keyring

            backend = keyring
        self._kr = backend

    def _service_name(self, service_key: str) -> str:
        return f"{self.prefix}.{service_key}"

    def get(self, service_key: str, field_name: str) -> str | None:
        try:
            return self._kr.get_password(self._service_name(service_key), field_name)
        except Exception:  # noqa: BLE001 — a locked keychain must not crash the app
            log.exception("keychain read failed for %s.%s", service_key, field_name)
            return None

    def set(self, service_key: str, field_name: str, value: str) -> None:
        if value:
            self._kr.set_password(self._service_name(service_key), field_name, value)
        else:
            self.delete(service_key, field_name)

    def delete(self, service_key: str, field_name: str) -> bool:
        try:
            self._kr.delete_password(self._service_name(service_key), field_name)
            return True
        except Exception:  # keyring raises PasswordDeleteError when absent
            return False

    def clear_service(self, service_key: str) -> int:
        spec = service(service_key)
        return sum(
            1 for f in spec.fields if f.secret and self.delete(service_key, f.name)
        )

    def wipe(self) -> int:
        return sum(self.clear_service(spec.key) for spec in SERVICES)


class FakeKeyring:
    """In-memory stand-in with keyring's exact surface. Used by the unit suite
    so tests never touch the real login keychain."""

    class PasswordDeleteError(Exception):
        pass

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise self.PasswordDeleteError(f"{service}/{username}")
        del self.store[(service, username)]


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "onboarding/complete": False,
    "downloads/dir": "",
    "downloads/ask_each_time": False,
    "stems/selection": list(config.DEFAULT_STEMS),
    #: Separations are written in their own format, chosen in the export
    #: dialog — a stem is not "the same file as the download" and remembering
    #: one choice for both made MP3 stems appear after a WAV download.
    "format/stems": config.WAV_48_24.key,
    "phrase/enabled": False,
    "phrase/candidates": config.PHRASE_CANDIDATES,
    "phrase/auto_trim": True,
    "phrase/fuzzy": True,
    #: Which engine each of the two swappable jobs uses. Both default to the
    #: free one, and both fall back to it when the paid one has no key.
    "phrase/engine": config.DEFAULT_PHRASE_ENGINE,
    "stems/engine": config.DEFAULT_STEM_ENGINE,
    "youtube/use_cookies": False,
    "ui/last_tab": "youtube",
    "ui/window_geometry": "",
    "soulseek/slskd_port": 5030,
}
_DEFAULTS.update({f"format/{p}": k for p, k in config.DEFAULT_FORMAT.items()})


@dataclass
class Settings:
    paths: config.Paths
    prefs: PrefBackend = field(default_factory=MemoryPrefs)
    secrets: SecretStore = field(default_factory=SecretStore)

    @classmethod
    def native(cls, paths: config.Paths | None = None) -> "Settings":
        paths = (paths or config.Paths.default()).ensure()
        return cls(paths=paths, prefs=QtPrefs(), secrets=SecretStore())

    @classmethod
    def headless(cls, paths: config.Paths) -> "Settings":
        """No Qt — for the phase-2 CLI and for integration tests.

        Headless mode must stay hermetic: it never touches the user's real
        keychain or system preferences when the app is being exercised from a
        shell or a test fixture.
        """
        paths.ensure()
        return cls(
            paths=paths,
            prefs=JsonPrefs(paths.support / "prefs.json"),
            secrets=SecretStore(backend=FakeKeyring()),
        )

    # -- generic prefs ---------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        raw = self.prefs.get_raw(key)
        if raw is None:
            return _DEFAULTS.get(key, default)
        try:
            return json.loads(raw)
        except ValueError:
            return raw  # a value written by an older build; treat as a string

    def set(self, key: str, value: Any) -> None:
        self.prefs.set_raw(key, json.dumps(value))

    def unset(self, key: str) -> None:
        self.prefs.remove(key)

    # -- typed accessors -------------------------------------------------

    @property
    def download_dir(self) -> Path:
        raw = self.get("downloads/dir")
        return Path(raw) if raw else self.paths.downloads

    @download_dir.setter
    def download_dir(self, value: Path | str) -> None:
        self.set("downloads/dir", str(value))

    @property
    def stem_selection(self) -> list[str]:
        """The checkbox picker remembers the last selection (build plan 1)."""
        keys = self.get("stems/selection") or list(config.DEFAULT_STEMS)
        valid = {opt.key for opt in config.STEM_OPTIONS}
        cleaned = [k for k in keys if k in valid]
        return cleaned or list(config.DEFAULT_STEMS)

    @stem_selection.setter
    def stem_selection(self, keys: list[str]) -> None:
        self.set("stems/selection", list(keys))

    def format_for(self, provider: str) -> config.OutputFormat:
        key = self.get(f"format/{provider}", config.DEFAULT_FORMAT.get(provider))
        try:
            fmt = config.format_by_key(key)
        except ValueError:
            fmt = config.format_by_key(config.DEFAULT_FORMAT[provider])
        if fmt not in config.formats_for(provider):
            fmt = config.format_by_key(config.DEFAULT_FORMAT[provider])
        return fmt

    def set_format_for(self, provider: str, fmt: config.OutputFormat | str) -> None:
        key = fmt if isinstance(fmt, str) else fmt.key
        config.format_by_key(key)  # raises on nonsense before it is persisted
        self.set(f"format/{provider}", key)

    # -- engines ---------------------------------------------------------

    def _engine(
        self, options: tuple[config.EngineOption, ...], key: str, default: str
    ) -> config.EngineOption:
        """The chosen engine, or the free one.

        Falls back for two reasons, and they are the same reason: an engine
        that cannot run is not a choice. A key written by a newer build, and a
        paid engine whose credential has since been cleared or wiped, both
        leave phrase search and separation working rather than broken.
        """
        chosen = str(self.get(key, default))
        try:
            option = config.engine(options, chosen)
        except ValueError:
            log.warning("unknown engine %r for %s; using %s", chosen, key, default)
            return config.engine(options, default)
        if option.service and not self.credential(option.service, "api_key"):
            return config.engine(options, default)
        return option

    @property
    def phrase_engine(self) -> config.EngineOption:
        return self._engine(
            config.PHRASE_ENGINES, "phrase/engine", config.DEFAULT_PHRASE_ENGINE
        )

    @phrase_engine.setter
    def phrase_engine(self, key: str) -> None:
        config.phrase_engine(key)  # raises on nonsense before it is persisted
        self.set("phrase/engine", key)

    @property
    def stem_engine(self) -> config.EngineOption:
        return self._engine(
            config.STEM_ENGINES, "stems/engine", config.DEFAULT_STEM_ENGINE
        )

    @stem_engine.setter
    def stem_engine(self, key: str) -> None:
        config.stem_engine(key)
        self.set("stems/engine", key)

    def engine_key(self, option: config.EngineOption) -> str | None:
        """The credential an engine runs on, or None when it needs none."""
        if option.service is None:
            return None
        return self.credential(option.service, "api_key")

    def engine_ready(self, option: config.EngineOption) -> bool:
        return option.service is None or bool(self.engine_key(option))

    @property
    def onboarding_complete(self) -> bool:
        return bool(self.get("onboarding/complete"))

    @onboarding_complete.setter
    def onboarding_complete(self, value: bool) -> None:
        self.set("onboarding/complete", bool(value))

    # -- credentials -----------------------------------------------------

    def _pref_key(self, service_key: str, field_name: str) -> str:
        return f"cred/{service_key}/{field_name}"

    def credential(self, service_key: str, field_name: str) -> str | None:
        spec = service(service_key)
        f = spec.field(field_name)
        if f.secret:
            return self.secrets.get(service_key, field_name)
        value = self.get(self._pref_key(service_key, field_name))
        return value or None

    def set_credential(self, service_key: str, field_name: str, value: str) -> None:
        spec = service(service_key)
        f = spec.field(field_name)
        if f.secret:
            self.secrets.set(service_key, field_name, value)
        elif value:
            self.set(self._pref_key(service_key, field_name), value)
        else:
            self.unset(self._pref_key(service_key, field_name))

    def credentials(self, service_key: str) -> dict[str, str | None]:
        spec = service(service_key)
        return {f.name: self.credential(service_key, f.name) for f in spec.fields}

    def clear_service(self, service_key: str) -> None:
        """The per-service 'Clear' button in the settings dialog."""
        spec = service(service_key)
        for f in spec.fields:
            if f.secret:
                self.secrets.delete(service_key, f.name)
            else:
                self.unset(self._pref_key(service_key, f.name))

    def is_configured(self, service_key: str) -> bool:
        spec = service(service_key)
        return all(
            self.credential(service_key, f.name) for f in spec.fields if f.required
        )

    def missing_required(self) -> Iterator[ServiceSpec]:
        for spec in SERVICES:
            if spec.required and not self.is_configured(spec.key):
                yield spec

    # -- wipe ------------------------------------------------------------

    def wipe_everything(self, *, cache: Any | None = None) -> dict[str, int]:
        """The single 'Wipe everything' action: Keychain entries, preferences,
        caches and download history, in one call (build plan 4).

        Files already saved to the user's download folder are deliberately not
        touched — those are their music, not our state.
        """
        removed = {"secrets": self.secrets.wipe(), "prefs": len(self.prefs.keys())}
        self.prefs.clear()

        if cache is not None:
            removed["cache_rows"] = cache.purge()
        else:
            db = self.paths.cache_db
            removed["cache_rows"] = 0
            if db.exists():
                from .core.cache import Cache

                with Cache(db) as c:
                    removed["cache_rows"] = c.purge()

        removed["temp_files"] = 0
        for directory in (self.paths.preview_dir, self.paths.clips_dir):
            if directory.exists():
                for p in directory.iterdir():
                    if p.is_file():
                        p.unlink()
                        removed["temp_files"] += 1
        return removed
