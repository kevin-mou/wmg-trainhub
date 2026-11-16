from __future__ import annotations

from trainhub.passwords import hash_password, password_is_set, verify_password


def test_password_hash_roundtrip_and_rejects_bad_input():
    encoded = hash_password("s3cret-pass")
    assert encoded.startswith("pbkdf2_sha256$")
    assert password_is_set(encoded)
    assert verify_password("s3cret-pass", encoded) is True
    assert verify_password("wrong-pass", encoded) is False
    assert verify_password("", encoded) is False
    assert verify_password("s3cret-pass", None) is False
    assert verify_password("s3cret-pass", "broken") is False

    for invalid in ("", "   ", "short"):
        try:
            hash_password(invalid)
        except ValueError:
            continue
        raise AssertionError(f"should reject {invalid!r}")
