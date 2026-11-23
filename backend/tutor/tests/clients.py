"""The platform app's test client, talking to it as the local browser does: a loopback base URL, so the Host header
is `127.0.0.1` (outside public mode the API refuses any other Host on /api/*; see `api.create_app`)."""
from fastapi.testclient import TestClient

LOCAL_BASE_URL = "http://127.0.0.1"


def local_client(app, **kw) -> TestClient:
    return TestClient(app, base_url=LOCAL_BASE_URL, **kw)
