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
        ("/assets/pages/notebook-editor.js", "renderNotebookEditor"),
        ("/assets/vendor/trainhub-codemirror.js", "createNotebookEditor"),
        ("/assets/vendor/trainhub-markdown.js", "renderMarkdown"),
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


async def test_static_assets_disable_stale_browser_cache(client):
    index = await client.get("/")
    assert "no-store" in index.headers["cache-control"]

    for path in ("/assets/app.js", "/assets/styles.css", "/assets/pages/notebook-editor.js"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers["cache-control"], path

    font = await client.get("/assets/fonts/instrument-sans-latin.woff2")
    assert font.status_code == 200
    assert "no-cache" in font.headers["cache-control"]
    assert "no-store" not in font.headers["cache-control"]
