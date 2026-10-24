from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_settings  # noqa: E402

from trainhub.config import Settings  # noqa: E402
from trainhub.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def db(app):
    return app.state.db


@pytest.fixture
def customer_token() -> str:
    return "trh_test_customer_token_1234567890abcdef"


@pytest.fixture
def customer(db, customer_token: str):
    return db.create_customer("Test Co", "test@example.com", customer_token)


@pytest.fixture
def headers(customer, customer_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {customer_token}"}


@pytest.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://trainhub.test") as http_client:
            yield http_client
