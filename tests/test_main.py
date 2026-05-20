from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)


def test_root():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_schema():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "paths" in response.json()
