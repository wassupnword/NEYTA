"""Preferences, the Keychain credential store, per-service clear and wipe.

Every test here runs against FakeKeyring. Nothing touches the real login
keychain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neyta import config, settings as S


class TestServiceDeclarations:
    def test_service_keys_are_unique(self):
        keys = [s.key for s in S.SERVICES]
        assert len(keys) == len(set(keys))

    def test_only_soulseek_is_required(self):
        required = [s.key for s in S.SERVICES if s.required]
        assert required == ["soulseek"]

    def test_youtube_is_optional_and_says_why(self):
        spec = S.service("youtube")
        assert not spec.required
        assert "off by default" in spec.note
        assert "account risk" in spec.note

    def test_youtube_cookie_field_is_a_file_the_user_exports(self):
        f = S.service("youtube").field("cookie_file")
        assert f.kind == "file"
        assert not f.secret  # a path is not a secret
        assert "browser" in f.help

    def test_passwords_and_tokens_are_declared_secret(self):
        assert S.service("soulseek").field("password").secret
        assert S.service("soundcloud").field("oauth_token").secret
        assert S.service("filmot").field("api_key").secret

    def test_folder_paths_are_not_secret(self):
        assert not S.service("soulseek").field("share_dir").secret

    def test_unknown_service_raises(self):
        with pytest.raises(KeyError):
            S.service("napster")

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError):
            S.service("soulseek").field("api_key")


class TestPrefs:
    def test_defaults_are_returned_for_unset_keys(self, settings):
        assert settings.get("phrase/candidates") == config.PHRASE_CANDIDATES
        assert settings.get("onboarding/complete") is False

    def test_roundtrip_preserves_type(self, settings):
        for key, value in [
            ("a/int", 7),
            ("a/bool", True),
            ("a/list", ["vocals", "drums"]),
            ("a/dict", {"x": 1}),
            ("a/str", "hello"),
            ("a/float", 1.5),
        ]:
            settings.set(key, value)
            assert settings.get(key) == value
            assert type(settings.get(key)) is type(value)

    def test_unset_restores_the_default(self, settings):
        settings.set("phrase/candidates", 5)
        settings.unset("phrase/candidates")
        assert settings.get("phrase/candidates") == config.PHRASE_CANDIDATES

    def test_explicit_default_wins_for_unknown_keys(self, settings):
        assert settings.get("nothing/here", "fallback") == "fallback"


class TestJsonPrefs:
    def test_survives_reopening(self, tmp_path):
        p = tmp_path / "prefs.json"
        S.JsonPrefs(p).set_raw("k", '"v"')
        assert S.JsonPrefs(p).get_raw("k") == '"v"'

    def test_corrupt_file_starts_fresh_rather_than_crashing(self, tmp_path):
        p = tmp_path / "prefs.json"
        p.write_text("{not json")
        assert S.JsonPrefs(p).keys() == []

    def test_write_is_atomic_leaving_no_temp_file(self, tmp_path):
        p = tmp_path / "prefs.json"
        S.JsonPrefs(p).set_raw("k", '"v"')
        assert list(tmp_path.iterdir()) == [p]


class TestTypedAccessors:
    def test_download_dir_falls_back_to_the_default_location(self, settings):
        assert settings.download_dir == settings.paths.downloads

    def test_download_dir_is_overridable(self, settings, tmp_path):
        settings.download_dir = tmp_path / "Samples"
        assert settings.download_dir == tmp_path / "Samples"
        assert isinstance(settings.download_dir, Path)

    def test_stem_selection_remembers_the_last_choice(self, settings):
        settings.stem_selection = ["vocals", "instrumental"]
        assert settings.stem_selection == ["vocals", "instrumental"]

    def test_stem_selection_drops_options_that_no_longer_exist(self, settings):
        settings.set("stems/selection", ["vocals", "nonexistent_preset"])
        assert settings.stem_selection == ["vocals"]

    def test_stem_selection_never_returns_empty(self, settings):
        settings.set("stems/selection", [])
        assert settings.stem_selection == list(config.DEFAULT_STEMS)

    def test_format_defaults_per_tab(self, settings):
        assert settings.format_for("youtube").key == config.WAV_48_24.key
        assert settings.format_for("soulseek").key == config.ORIGINAL.key

    def test_format_is_remembered(self, settings):
        settings.set_format_for("youtube", config.MP3_320)
        assert settings.format_for("youtube").key == "mp3_320"

    def test_setting_a_nonsense_format_raises_rather_than_persisting(self, settings):
        with pytest.raises(ValueError):
            settings.set_format_for("youtube", "flac_1411")
        assert settings.format_for("youtube").key == config.WAV_48_24.key

    def test_a_format_from_another_tab_falls_back_to_the_default(self, settings):
        # MP4 exists, but not on the SoundCloud tab.
        settings.set("format/soundcloud", "mp4_video")
        assert settings.format_for("soundcloud").key == config.WAV_48_24.key

    def test_a_stale_stored_format_falls_back(self, settings):
        settings.set("format/youtube", "ogg_from_an_older_build")
        assert settings.format_for("youtube").key == config.WAV_48_24.key


class TestCredentials:
    def test_secret_fields_go_to_the_keychain_not_the_prefs(
        self, settings, fake_keyring
    ):
        settings.set_credential("soulseek", "password", "hunter2")
        assert fake_keyring.store[("NEYTA.soulseek", "password")] == "hunter2"
        assert not any("hunter2" in (settings.prefs.get_raw(k) or "")
                       for k in settings.prefs.keys())

    def test_non_secret_fields_go_to_the_prefs_not_the_keychain(
        self, settings, fake_keyring
    ):
        settings.set_credential("soulseek", "username", "yesman")
        assert settings.credential("soulseek", "username") == "yesman"
        assert ("NEYTA.soulseek", "username") not in fake_keyring.store

    def test_roundtrip_for_every_declared_field(self, settings):
        for spec in S.SERVICES:
            for f in spec.fields:
                settings.set_credential(spec.key, f.name, f"value-{f.name}")
                assert settings.credential(spec.key, f.name) == f"value-{f.name}"

    def test_unset_credentials_are_none(self, settings):
        assert settings.credential("soundcloud", "oauth_token") is None

    def test_empty_string_clears_rather_than_storing_blank(self, settings):
        settings.set_credential("soulseek", "password", "x")
        settings.set_credential("soulseek", "password", "")
        assert settings.credential("soulseek", "password") is None

    def test_credentials_returns_the_whole_service(self, settings):
        settings.set_credential("soulseek", "username", "u")
        creds = settings.credentials("soulseek")
        assert set(creds) == {"username", "password", "share_dir"}
        assert creds["username"] == "u"
        assert creds["password"] is None

    def test_unknown_service_raises(self, settings):
        with pytest.raises(KeyError):
            settings.set_credential("napster", "password", "x")

    def test_a_locked_keychain_reads_as_none_rather_than_crashing(self, settings):
        class Locked:
            def get_password(self, *a):
                raise RuntimeError("keychain is locked")

        settings.secrets = S.SecretStore(backend=Locked())
        assert settings.credential("soulseek", "password") is None


class TestConfigured:
    def test_soulseek_needs_all_three_required_fields(self, settings, tmp_path):
        assert not settings.is_configured("soulseek")
        settings.set_credential("soulseek", "username", "u")
        settings.set_credential("soulseek", "password", "p")
        assert not settings.is_configured("soulseek")
        settings.set_credential("soulseek", "share_dir", str(tmp_path))
        assert settings.is_configured("soulseek")

    def test_optional_services_are_configured_by_default(self, settings):
        # Nothing is required, so nothing is missing. YouTube and SoundCloud
        # work unauthenticated.
        assert settings.is_configured("youtube")
        assert settings.is_configured("soundcloud")

    def test_missing_required_lists_only_soulseek(self, settings):
        assert [s.key for s in settings.missing_required()] == ["soulseek"]

    def test_missing_required_empties_once_soulseek_is_set(self, settings, tmp_path):
        for name, value in [("username", "u"), ("password", "p"),
                            ("share_dir", str(tmp_path))]:
            settings.set_credential("soulseek", name, value)
        assert list(settings.missing_required()) == []


class TestClearAndWipe:
    def test_clear_service_removes_both_kinds_of_field(self, settings, tmp_path):
        settings.set_credential("soulseek", "username", "u")
        settings.set_credential("soulseek", "password", "p")
        settings.set_credential("soulseek", "share_dir", str(tmp_path))
        settings.clear_service("soulseek")
        assert settings.credentials("soulseek") == {
            "username": None, "password": None, "share_dir": None
        }

    def test_clear_service_leaves_other_services_alone(self, settings):
        settings.set_credential("soulseek", "password", "p")
        settings.set_credential("filmot", "api_key", "k")
        settings.clear_service("soulseek")
        assert settings.credential("filmot", "api_key") == "k"

    def test_clearing_an_empty_service_is_not_an_error(self, settings):
        settings.clear_service("discogs")


class TestEngineChoice:
    """Which engine does the two swappable jobs, and when it is allowed to."""

    def test_both_default_to_the_free_engine(self, settings):
        assert settings.phrase_engine.key == "builtin"
        assert settings.stem_engine.key == "uvr"
        assert settings.phrase_engine.free and settings.stem_engine.free

    def test_a_paid_engine_needs_its_key_before_it_takes_effect(self, settings):
        settings.phrase_engine = "filmot"
        # Chosen, and stored as chosen...
        assert settings.get("phrase/engine") == "filmot"
        # ...but not in force, because it cannot run without a key.
        assert settings.phrase_engine.key == "builtin"

        settings.set_credential("filmot", "api_key", "k")
        assert settings.phrase_engine.key == "filmot"

    def test_clearing_the_key_falls_back_rather_than_breaking(self, settings):
        settings.stem_engine = "lalal"
        settings.set_credential("lalal", "api_key", "licence")
        assert settings.stem_engine.key == "lalal"
        settings.clear_service("lalal")
        assert settings.stem_engine.key == "uvr"

    def test_a_wipe_leaves_both_jobs_working(self, settings):
        settings.phrase_engine = "filmot"
        settings.set_credential("filmot", "api_key", "k")
        settings.wipe_everything()
        assert settings.phrase_engine.key == "builtin"
        assert settings.stem_engine.key == "uvr"

    def test_a_value_from_a_newer_build_does_not_break_the_older_one(
        self, settings
    ):
        settings.set("stems/engine", "something-from-the-future")
        assert settings.stem_engine.key == "uvr"

    def test_nonsense_is_refused_before_it_is_persisted(self, settings):
        with pytest.raises(ValueError):
            settings.stem_engine = "nope"
        assert settings.get("stems/engine") == "uvr"

    def test_every_paid_engine_names_a_service_that_holds_a_key(self):
        # `engine_key` looks up "api_key" on the named service, so a paid
        # engine pointed at a service without one would silently never be
        # ready. This is the coupling that keeps that from happening quietly.
        paid = [
            o for o in (*config.PHRASE_ENGINES, *config.STEM_ENGINES) if o.paid
        ]
        assert paid
        for option in paid:
            assert S.service(option.service).field("api_key").secret

    def test_no_free_engine_needs_a_service(self):
        for option in (*config.PHRASE_ENGINES, *config.STEM_ENGINES):
            if option.free:
                assert option.service is None

    def test_the_key_an_engine_runs_on_is_reported(self, settings):
        settings.set_credential("lalal", "api_key", "licence")
        assert settings.engine_key(config.stem_engine("lalal")) == "licence"
        assert settings.engine_key(config.stem_engine("uvr")) is None
        assert settings.engine_ready(config.stem_engine("uvr"))

    def test_wipe_empties_the_keychain(self, settings, fake_keyring):
        for spec in S.SERVICES:
            for f in spec.fields:
                settings.set_credential(spec.key, f.name, "x")
        settings.wipe_everything()
        assert fake_keyring.store == {}

    def test_wipe_empties_the_prefs(self, settings):
        settings.set("ui/last_tab", "soulseek")
        settings.wipe_everything()
        assert settings.get("ui/last_tab") == "youtube"  # back to the default

    def test_wipe_clears_the_cache(self, settings, disk_cache):
        disk_cache.put_captions("v", "en", {"segs": []})
        removed = settings.wipe_everything(cache=disk_cache)
        assert removed["cache_rows"] == 1
        assert disk_cache.count() == 0

    def test_wipe_clears_temp_media(self, settings):
        (settings.paths.preview_dir / "peer.flac").write_bytes(b"x")
        (settings.paths.clips_dir / "clip.wav").write_bytes(b"x")
        removed = settings.wipe_everything()
        assert removed["temp_files"] == 2
        assert list(settings.paths.preview_dir.iterdir()) == []

    def test_wipe_does_not_touch_the_users_finished_files(self, settings):
        keeper = settings.paths.downloads / "A - B [vocals].wav"
        keeper.write_bytes(b"music")
        settings.wipe_everything()
        assert keeper.exists()

    def test_wipe_reports_what_it_removed(self, settings):
        settings.set_credential("soulseek", "password", "p")
        settings.set("ui/last_tab", "soulseek")
        removed = settings.wipe_everything()
        assert removed["secrets"] >= 1
        assert removed["prefs"] >= 1


class TestSecretStore:
    def test_service_names_are_namespaced_for_keychain_access(self, fake_keyring):
        store = S.SecretStore(backend=fake_keyring)
        store.set("soulseek", "password", "p")
        assert ("NEYTA.soulseek", "password") in fake_keyring.store

    def test_deleting_an_absent_secret_is_false_not_an_error(self, fake_keyring):
        store = S.SecretStore(backend=fake_keyring)
        assert store.delete("soulseek", "password") is False

    def test_wipe_walks_the_declaration_so_nothing_is_orphaned(self, fake_keyring):
        store = S.SecretStore(backend=fake_keyring)
        secret_fields = [
            (s.key, f.name) for s in S.SERVICES for f in s.fields if f.secret
        ]
        for service_key, field_name in secret_fields:
            store.set(service_key, field_name, "x")
        assert store.wipe() == len(secret_fields)
        assert fake_keyring.store == {}
