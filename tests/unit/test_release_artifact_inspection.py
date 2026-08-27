"""Regression tests for Windows release artifact inspection."""

# pyright: reportPrivateUsage=false

from pathlib import Path

from scripts.inspect_release_artifact import _contains_secret, _is_forbidden


def test_certifi_public_ca_bundle_is_allowed_but_private_pem_is_rejected() -> None:
    assert not _is_forbidden(Path("_internal/certifi/cacert.pem"))
    assert _is_forbidden(Path("_internal/customer-private.pem"))


def test_python_database_expression_is_not_treated_as_literal_secret(tmp_path: Path) -> None:
    migration = tmp_path / "migration.py"
    migration.write_text(
        'values = {"api_key": str(legacy["api_key"] or "")}\n',
        encoding="utf-8",
    )

    assert not _contains_secret(migration)


def test_python_literal_api_key_is_still_rejected(tmp_path: Path) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        'api_key = "sk-release-secret-1234567890"\n',
        encoding="utf-8",
    )

    assert _contains_secret(source)
