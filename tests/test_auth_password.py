from __future__ import annotations

ADMIN = {"X-Admin-Token": "admin-test-token"}
PASSWORD = "strong-pass-123"


async def _create_customer(client, *, name="Password Co", email="User@Example.com", username=None):
    payload = {"name": name, "email": email}
    if username is not None:
        payload["username"] = username
    response = await client.post("/api/admin/customers", headers=ADMIN, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_new_customer_defaults_to_token_only_login(client):
    customer = await _create_customer(client)
    assert customer["username"] == "user@example.com"
    assert customer["hasPassword"] is False

    token_login = await client.post("/api/auth/login", json={"token": customer["token"]})
    assert token_login.status_code == 200, token_login.text
    assert token_login.json()["data"]["token"] == customer["token"]

    password_login = await client.post(
        "/api/auth/login",
        json={"username": "USER@example.com", "password": "whatever-123"},
    )
    assert password_login.status_code == 401
    assert "尚未设置密码" in password_login.json()["message"]


async def test_set_password_then_login_and_update(client):
    customer = await _create_customer(client)
    headers = {"Authorization": f"Bearer {customer['token']}"}

    first = await client.put("/api/auth/password", headers=headers, json={"newPassword": PASSWORD})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["hasPassword"] is True

    login = await client.post(
        "/api/auth/login", json={"username": customer["username"], "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["data"]["token"] == customer["token"]

    wrong = await client.post(
        "/api/auth/login",
        json={"username": customer["username"], "password": "wrong-pass-123"},
    )
    assert wrong.status_code == 401

    no_current = await client.put(
        "/api/auth/password", headers=headers, json={"newPassword": "another-pass-456"}
    )
    assert no_current.status_code == 400
    assert "当前密码" in no_current.json()["message"]

    bad_current = await client.put(
        "/api/auth/password",
        headers=headers,
        json={"currentPassword": "wrong-pass-123", "newPassword": "another-pass-456"},
    )
    assert bad_current.status_code == 400

    changed = await client.put(
        "/api/auth/password",
        headers=headers,
        json={"currentPassword": PASSWORD, "newPassword": "another-pass-456"},
    )
    assert changed.status_code == 200, changed.text

    old = await client.post(
        "/api/auth/login", json={"username": customer["username"], "password": PASSWORD}
    )
    assert old.status_code == 401
    new = await client.post(
        "/api/auth/login",
        json={"username": customer["username"], "password": "another-pass-456"},
    )
    assert new.status_code == 200, new.text

    # 密码更新不影响访问令牌
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["hasPassword"] is True


async def test_short_password_rejected(client):
    customer = await _create_customer(client)
    headers = {"Authorization": f"Bearer {customer['token']}"}
    response = await client.put(
        "/api/auth/password", headers=headers, json={"newPassword": "short"}
    )
    assert response.status_code == 422


async def test_username_defaults_to_customer_id_without_email(client, db):
    customer = db.create_customer("No Email Co", None, "trh_password_no_email_token_123456")
    assert customer["username"] == customer["id"]


async def test_duplicate_username_rejected(client):
    await _create_customer(client, name="First", email=None, username="shared-name")
    duplicate = await client.post(
        "/api/admin/customers",
        headers=ADMIN,
        json={"name": "Second", "username": "Shared-Name"},
    )
    assert duplicate.status_code == 409
    assert "用户名" in duplicate.json()["message"]


async def test_disabled_customer_cannot_password_login(client):
    customer = await _create_customer(client)
    headers = {"Authorization": f"Bearer {customer['token']}"}
    set_password = await client.put(
        "/api/auth/password", headers=headers, json={"newPassword": PASSWORD}
    )
    assert set_password.status_code == 200
    disabled = await client.post(
        f"/api/admin/customers/{customer['id']}/status",
        headers=ADMIN,
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200

    login = await client.post(
        "/api/auth/login", json={"username": customer["username"], "password": PASSWORD}
    )
    assert login.status_code == 403


async def test_password_login_requires_both_fields(client):
    response = await client.post("/api/auth/login", json={"username": "someone"})
    assert response.status_code == 400
    response = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 400
