from __future__ import annotations


async def test_spa_index_is_served(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "TrainHub" in response.text
    assert "/assets/app.js" in response.text


async def test_static_assets_are_served(client):
    for path, marker in (
        ("/assets/app.js", "const NAV_ITEMS"),
        ("/assets/styles.css", "--pine"),
        ("/assets/i18n.js", "dictionaries"),
        ("/assets/metrics-chart.js", "renderMetricsChart"),
        ("/assets/fonts/instrument-sans-latin.woff2", None),
    ):
        response = await client.get(path)
        assert response.status_code == 200, path
        if marker:
            assert marker in response.text, path


async def test_unknown_api_route_returns_envelope(client, headers):
    response = await client.get("/api/does-not-exist", headers=headers)
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 404
    assert "message" in body
