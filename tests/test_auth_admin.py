from __future__ import annotations

import pytest


async def test_healthz_is_public(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_api_requires_token(client):
    response = await client.get("/api/overview")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 401
    assert "令牌" in body["message"]


async def test_api_rejects_unknown_token(client):
    response = await client.get("/api/overview", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_customer_session_and_disabled_account(client, db, customer, headers):
    response = await client.get("/api/auth/me", headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "Test Co"
    assert data["tokenMasked"].startswith("trh_")
    assert "token" not in data

    db.update_customer_status(customer["id"], "disabled")
    response = await client.get("/api/overview", headers=headers)
    assert response.status_code == 403
    assert "停用" in response.json()["message"]


async def test_admin_endpoints_require_admin_token(client, headers):
    response = await client.get("/api/admin/customers", headers=headers)
    assert response.status_code == 401

    response = await client.get("/api/admin/customers")
    assert response.status_code == 401


async def test_admin_creates_customer_and_rotates_token(client, headers):
    created = await client.post(
        "/api/admin/customers",
        headers={"X-Admin-Token": "admin-test-token"},
        json={"name": "Acme Labs", "email": "ops@acme.test"},
    )
    assert created.status_code == 200
    payload = created.json()["data"]
    assert payload["token"].startswith("trh_")
    token = payload["token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["name"] == "Acme Labs"

    rotated = await client.post(
        f"/api/admin/customers/{payload['id']}/rotate-token",
        headers={"X-Admin-Token": "admin-test-token"},
    )
    new_token = rotated.json()["data"]["token"]
    assert new_token and new_token != token

    old = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert old.status_code == 401
    fresh = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert fresh.status_code == 200

    disabled = await client.post(
        f"/api/admin/customers/{payload['id']}/status",
        headers={"X-Admin-Token": "admin-test-token"},
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    blocked = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert blocked.status_code == 403

    deleted = await client.delete(
        f"/api/admin/customers/{payload['id']}",
        headers={"X-Admin-Token": "admin-test-token"},
    )
    assert deleted.status_code == 200
    gone = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert gone.status_code == 401


async def test_admin_api_disabled_without_token(tmp_path):
    from helpers import make_settings
    from httpx import ASGITransport, AsyncClient

    from trainhub.main import create_app

    app = create_app(make_settings(tmp_path, admin_token=None))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://trainhub.test") as client:
            response = await client.get(
                "/api/admin/customers", headers={"X-Admin-Token": "anything"}
            )
            assert response.status_code == 503


@pytest.mark.parametrize("path", ["/api/datasets", "/api/notebooks", "/api/runs", "/api/activity"])
async def test_business_endpoints_require_token(client, path):
    response = await client.get(path)
    assert response.status_code == 401
