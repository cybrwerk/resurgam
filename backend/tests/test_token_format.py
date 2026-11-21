import pytest

from resurgam_api.connectivity.token_format import (
    assemble,
    normalize_combined,
    token_id_error,
    token_id_from_stored,
    token_secret_error,
)

SECRET = "d63e505a-e3ec-449a-9bc7-1da610d4ccde"


def test_assemble_uses_product_separator():
    assert assemble("root@pam!resurgam", SECRET, "pbs") == f"root@pam!resurgam:{SECRET}"
    assert assemble(" root@pam!resurgam ", f" {SECRET} ", "pve") == f"root@pam!resurgam={SECRET}"


@pytest.mark.parametrize(
    ("value", "product", "expected"),
    [
        (f"root@pam!t:{SECRET}", "pve", f"root@pam!t={SECRET}"),  # the real user's 401
        (f"root@pam!t={SECRET}", "pbs", f"root@pam!t:{SECRET}"),
        (f"root@pam!t={SECRET}", "pve", f"root@pam!t={SECRET}"),
        ("x=y", "pve", "x=y"),  # unparseable legacy input kept verbatim
    ],
)
def test_normalize_combined(value, product, expected):
    assert normalize_combined(value, product) == expected


@pytest.mark.parametrize("value", ["root@pam!resurgam", "resurgam@pbs!dev-1", "svc.backup@ldap-corp!a_b.c"])
def test_valid_token_ids(value):
    assert token_id_error(value, "pve") is None


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("root@pam", "'!'"),
        (f"root@pam!t={SECRET}", "must not contain the secret"),
        ("root!t", "realm"),
        ("root@pam!", "not valid"),
    ],
)
def test_invalid_token_ids_explain_format(value, fragment):
    error = token_id_error(value, "pve")
    assert fragment in error
    assert "user@realm!tokenname" in error
    assert SECRET not in error


def test_secret_validation():
    assert token_secret_error(SECRET) is None
    assert token_secret_error("not-a-uuid-but-fine") is None
    assert "empty" in token_secret_error("  ")
    assert "spaces" in token_secret_error("a b")
    assert "full token" in token_secret_error(f"root@pam!t={SECRET}")


def test_token_id_from_stored_never_leaks_secret():
    assert token_id_from_stored(f"root@pam!t:{SECRET}") == "root@pam!t"
    assert token_id_from_stored(f"root@pam!t={SECRET}") == "root@pam!t"
    assert token_id_from_stored("garbage") is None
    assert token_id_from_stored(None) is None


def test_bare_token_id_placeholder_keeps_its_id_and_counts_as_secret_missing():
    from resurgam_api.connectivity.token_format import token_secret_missing

    # what a configuration import stores for a connection whose secret was never exported
    assert token_id_from_stored("root@pam!resurgam") == "root@pam!resurgam"
    assert token_secret_missing("root@pam!resurgam") is True
    assert token_secret_missing("") is True
    assert token_secret_missing(None) is True
    assert token_secret_missing(f"root@pam!t:{SECRET}") is False
    assert token_secret_missing(f"root@pam!t={SECRET}") is False
