import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest
from dotenv import dotenv_values

from gateway.config import ConfigStore, SESSION_COOKIE, _DEFAULTS, normalize_cookie
from gateway.errors import GatewayError


COOKIE = f"{SESSION_COOKIE}=synthetic-session-alpha"
OTHER = f"{SESSION_COOKIE}=synthetic-session-beta"
ROTATED = f"{SESSION_COOKIE}=synthetic-session-rotated"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in _DEFAULTS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def store(tmp_path):
    return ConfigStore(tmp_path / ".env")


def test_defaults_immutable_snapshots_and_permissions(store):
    assert store.settings.host == "127.0.0.1"
    assert store.settings.port == 8000
    assert store.settings.api_token == "123456"
    assert store.settings.upstream_timeout == 1800
    assert store.settings.connect_timeout == 15
    assert store.settings.data_dir == store.path.parent / "data"
    assert store.settings.enabled_toolsets == ()
    assert store.accounts() == []
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert "123456" not in repr(store.settings)
    with pytest.raises(FrozenInstanceError):
        store.settings.port = 9000
    account_id = store.add_accounts(COOKIE, enabled=False)["ids"][0]
    records = store.accounts()
    records[0]["cookie"] = "corrupt"
    disabled = store.disabled_ids
    disabled.clear()
    assert store.accounts()[0]["cookie"] == COOKIE
    assert store.disabled_ids == {account_id}


def test_file_precedence_environment_import_and_no_stale_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "external-key")
    monkeypatch.setenv("PORT", "8123")
    path = tmp_path / ".env"
    path.write_text("API_TOKEN='file-key'\nUNRELATED='literal ${PORT}'\n# retained comment\n")
    store = ConfigStore(path)
    assert store.settings.api_token == "file-key"
    assert store.settings.port == 8123
    # Disk changed after startup: the transaction must read it again, not os.environ.
    path.write_text(path.read_text().replace("PORT='8123'", "PORT='8999'"))
    monkeypatch.setenv("PORT", "8444")
    store.set_default_model("model-${PORT}")
    assert store.settings.port == 8999
    assert store.settings.default_model == "model-${PORT}"
    assert "UNRELATED='literal ${PORT}'" in path.read_text()
    assert "# retained comment" in path.read_text()
    assert ConfigStore(path).settings.default_model == "model-${PORT}"


def test_literal_quotes_backslashes_multiline_and_unknown_binding_round_trip(tmp_path):
    path = tmp_path / ".env"
    unknown = 'EXTRA="first line\nsecond \\\"quoted\\\" line"\n'
    path.write_text(unknown)
    store = ConfigStore(path)
    model = "model'with\\literal\nand ${UNEXPANDED} \"text\""
    store.set_default_model(model)
    assert ConfigStore(path).settings.default_model == model
    assert unknown in path.read_text()


@pytest.mark.parametrize("text", [
    f"Cookie: ignored=tracking; {SESSION_COOKIE}.1=second; {SESSION_COOKIE}.0=first",
    f"'{SESSION_COOKIE}.0=first; {SESSION_COOKIE}.1=second'",
    f"{SESSION_COOKIE}.0=first; {SESSION_COOKIE}.0=first; {SESSION_COOKIE}.1=second; other=value",
])
def test_normalize_cookie_chunks(text):
    assert normalize_cookie(text) == f"{SESSION_COOKIE}.0=first; {SESSION_COOKIE}.1=second"


@pytest.mark.parametrize("text", [
    "tracking=very-private", "next-auth.session-token=very-private",
    f"{SESSION_COOKIE}.1=very-private", f"{SESSION_COOKIE}.0=x; {SESSION_COOKIE}.2=very-private",
    f"{SESSION_COOKIE}=very-private; {SESSION_COOKIE}.0=x",
    f"{SESSION_COOKIE}.0=x; {SESSION_COOKIE}.0=very-private",
    f"{SESSION_COOKIE}.00=very-private", f"{SESSION_COOKIE}.bad=very-private",
    f"{SESSION_COOKIE}.{'9' * 5000}=very-private",
    f"{SESSION_COOKIE}=", f"{SESSION_COOKIE}=very-private bad",
    f"{SESSION_COOKIE}=very-private\r\nInjected: true", f"{SESSION_COOKIE}=very-private,other",
])
def test_bad_cookie_is_safe(text):
    with pytest.raises(GatewayError) as caught:
        normalize_cookie(text, index=3)
    assert caught.value.status == 400
    assert "第 3 条" in str(caught.value)
    assert "very-private" not in str(caught.value)


def test_import_normalizes_deduplicates_and_clears_one_shot_source(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"DIALX_COOKIES='Cookie: tracking=x; {COOKIE}\n{OTHER}|||{COOKIE}'\n")
    store = ConfigStore(path)
    assert len(store.accounts()) == 2
    values = dotenv_values(path, interpolate=False)
    assert values["DIALX_COOKIES"] == ""
    assert len(json.loads(values["DIALX_ACCOUNTS"])) == 2
    assert store.add_accounts(f'{COOKIE}\n{OTHER}|||{COOKIE}') == {"added": 0, "duplicates": 3, "ids": []}
    store.delete_accounts([record["id"] for record in store.accounts()])
    assert ConfigStore(path).accounts() == []


def test_malformed_batch_is_all_or_nothing(store):
    before = store.path.read_bytes()
    with pytest.raises(GatewayError):
        store.add_accounts(COOKIE + "\ninvalid-secret")
    assert store.accounts() == []
    assert store.path.read_bytes() == before
    with pytest.raises(GatewayError):
        store.add_accounts("\n|||")


def test_rotation_preserves_ids_generation_disabled_and_restart(store):
    account_id = store.add_accounts(COOKIE, enabled=False)["ids"][0]
    original = store.accounts()[0]
    store.set_default_model("dynamic-model")
    assert store.refresh_cookie(account_id, original["generation"], ROTATED)
    restarted = ConfigStore(store.path)
    assert restarted.accounts()[0] == {**original, "cookie": ROTATED}
    assert restarted.disabled_ids == {account_id}
    assert restarted.settings.default_model == "dynamic-model"
    assert restarted.add_accounts(ROTATED)["duplicates"] == 1


def test_delete_readd_generation_rejects_late_response(store):
    account_id = store.add_accounts(COOKIE)["ids"][0]
    generation = store.accounts()[0]["generation"]
    assert store.delete_accounts([account_id]) == 1
    assert not store.refresh_cookie(account_id, generation, "")
    assert store.accounts() == []
    assert store.add_accounts(COOKIE)["ids"] == [account_id]
    assert store.accounts()[0]["generation"] != generation
    assert not store.refresh_cookie(account_id, generation, ROTATED)
    assert store.accounts()[0]["cookie"] == COOKIE


def test_empty_rotation_raises_and_does_not_corrupt_record(store):
    account_id = store.add_accounts(COOKIE)["ids"][0]
    record = store.accounts()[0]
    with pytest.raises(GatewayError) as caught:
        store.refresh_cookie(account_id, record["generation"], "")
    assert caught.value.code == "invalid_cookie"
    assert store.accounts()[0] == record


@pytest.mark.parametrize("key,value", [
    ("API_TOKEN", ""), ("API_TOKEN", "one,two"), ("API_TOKEN", "one|||two"),
    ("API_TOKEN", "one two"), ("PORT", "65536"), ("PORT", "oops-secret"),
    ("UPSTREAM_TIMEOUT", "nan"), ("CONNECT_TIMEOUT", "0"), ("ACCOUNT_WAIT_SECONDS", "-1"),
    ("MODEL_CACHE_TTL", "1.5"), ("LOG_RETENTION_DAYS", "0"),
    ("DIALX_ENABLED_TOOLSETS", '["unknown-tool"]'), ("COGITO_DISABLED", "not-json-secret"),
    ("DIALX_ACCOUNTS", "{}"), ("MAX_RESPONSE_BYTES", "1"),
])
def test_invalid_config_values_safe(tmp_path, key, value):
    path = tmp_path / ".env"
    path.write_text(f"{key}='{value}'\n")
    with pytest.raises(GatewayError) as caught:
        ConfigStore(path)
    assert caught.value.status == 400
    assert "oops-secret" not in str(caught.value)
    assert "not-json-secret" not in str(caught.value)


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_persistence_failure_never_publishes_partial_state(store, monkeypatch, operation):
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError("synthetic-private-filesystem-details")

    with monkeypatch.context() as patch:
        patch.setattr(os, operation, fail)
        with pytest.raises(GatewayError) as caught:
            store.add_accounts(COOKIE)
        assert caught.value.code == "config_persistence_failed"
        assert "synthetic-private" not in str(caught.value)
    assert store.accounts() == []
    assert store.path.read_bytes() == before
    assert list(store.path.parent.glob("..env.*")) == []
    assert store.add_accounts(COOKIE)["added"] == 1


def test_atomic_replace_is_private_and_complete_before_publication(store, monkeypatch):
    original_replace = os.replace
    observed = []

    def inspect(source, target):
        from pathlib import Path
        temporary = Path(source)
        assert temporary.stat().st_mode & 0o777 == 0o600
        assert store.accounts() == []
        parsed = dotenv_values(temporary, interpolate=False)
        assert json.loads(parsed["DIALX_ACCOUNTS"])[0]["cookie"] == COOKIE
        observed.append(True)
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", inspect)
    store.add_accounts(COOKIE)
    assert observed == [True]


def test_independent_store_transactions_merge_latest_disk_state(store):
    other = ConfigStore(store.path)
    account_id = store.add_accounts(COOKIE)["ids"][0]
    generation = store.accounts()[0]["generation"]
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = [
            workers.submit(store.refresh_cookie, account_id, generation, ROTATED),
            workers.submit(other.set_enabled, account_id, False),
            workers.submit(other.set_default_model, "concurrent-model"),
        ]
        for future in futures:
            future.result()
    latest = ConfigStore(store.path)
    assert latest.accounts()[0]["cookie"] == ROTATED
    assert latest.disabled_ids == {account_id}
    assert latest.settings.default_model == "concurrent-model"


def test_concurrent_duplicate_additions_are_deduplicated(store):
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: store.add_accounts(COOKIE), range(16)))
    assert sum(result["added"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 15
    assert len(ConfigStore(store.path).accounts()) == 1


def test_secret_values_include_full_cookies_and_each_chunk(store):
    cookie = f"{SESSION_COOKIE}.0=first-value; {SESSION_COOKIE}.1=second-value"
    store.add_accounts(cookie)
    secrets = store.secret_values()
    assert {"123456", cookie, "first-value", "second-value", "first-valuesecond-value"} <= set(secrets)


def test_missing_account_toggle_404_and_delete_idempotent(store):
    with pytest.raises(GatewayError) as caught:
        store.set_enabled("acc_missing", False)
    assert caught.value.status == 404
    assert store.delete_accounts(["acc_missing"]) == 0


def test_corrupt_dotenv_does_not_echo_input(tmp_path):
    path = tmp_path / ".env"
    path.write_text("API_TOKEN='unterminated-private-secret\n")
    with pytest.raises(GatewayError) as caught:
        ConfigStore(path)
    assert "private-secret" not in str(caught.value)


def test_published_snapshot_reads_do_not_wait_for_disk_transaction(store, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    original = store._write

    def blocked(*args):
        entered.set()
        assert finish.wait(2)
        original(*args)

    monkeypatch.setattr(store, "_write", blocked)
    with ThreadPoolExecutor(max_workers=2) as workers:
        writing = workers.submit(store.add_accounts, COOKIE)
        assert entered.wait(2)
        try:
            reading = workers.submit(lambda: (store.settings.port, store.accounts(), store.disabled_ids))
            assert reading.result(timeout=0.2) == (8000, [], set())
        finally:
            finish.set()
        writing.result(timeout=2)


def test_initial_import_respects_matching_disabled_stable_id(tmp_path):
    import hashlib
    account_id = "acc_" + hashlib.sha256(COOKIE.encode()).hexdigest()[:16]
    path = tmp_path / ".env"
    path.write_text(f"DIALX_COOKIES='{COOKIE}'\nCOGITO_DISABLED='[\"{account_id}\"]'\n")
    store = ConfigStore(path)
    assert store.disabled_ids == {account_id}
    assert ConfigStore(path).disabled_ids == {account_id}


def test_example_config_loads_with_no_accounts(tmp_path):
    from pathlib import Path
    path = tmp_path / ".env"
    path.write_text(Path(".env.example").read_text())
    store = ConfigStore(path)
    assert store.settings.api_token == "123456"
    assert store.accounts() == []


def test_environment_cookie_import_does_not_resurrect_deleted_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DIALX_COOKIES", COOKIE)
    store = ConfigStore(tmp_path / ".env")
    store.delete_accounts([store.accounts()[0]["id"]])
    assert ConfigStore(store.path).accounts() == []
