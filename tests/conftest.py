import os
import sys
import tempfile
from pathlib import Path

import pytest

# Test environment must exist before server.py is imported
_tmp = tempfile.mkdtemp(prefix="accent-test-")
os.environ["DB_DIR"] = _tmp
os.environ["OPENAI_API_KEY"] = "test-openai-key"
os.environ["ELEVENLABS_API_KEY"] = "test-elevenlabs-key"
os.environ["SECRET_KEY"] = "test-secret-key-for-sessions"
os.environ["ADMIN_EMAILS"] = "admin@test.com"
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Start every test with an empty database."""
    with server.db() as conn:
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM conversions")
    yield


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


def signup(client, email="user@test.com", password="password123", ip=None):
    headers = {"x-forwarded-for": ip} if ip else {}
    return client.post(
        "/api/signup",
        data={"email": email, "password": password},
        headers=headers,
    )


@pytest.fixture()
def fake_voice_apis(monkeypatch):
    """Replace ElevenLabs and OpenAI calls with instant fakes."""
    from types import SimpleNamespace

    deleted = []

    monkeypatch.setattr(
        server.el_client.voices.ivc, "create",
        lambda **kw: SimpleNamespace(voice_id="fake-voice-id"),
    )
    monkeypatch.setattr(
        server.el_client.voices, "delete",
        lambda vid: deleted.append(vid),
    )
    monkeypatch.setattr(
        server.openai_client.audio.transcriptions, "create",
        lambda **kw: SimpleNamespace(text="Hello world"),
    )
    monkeypatch.setattr(
        server.el_client.text_to_speech, "convert",
        lambda **kw: iter([b"fake-mp3-bytes"]),
    )
    return {"deleted_voices": deleted}


def clone(client, size=200_000, consent="yes"):
    return client.post(
        "/api/clone",
        data={"consent": consent},
        files={"audio": ("sample.webm", b"x" * size, "audio/webm")},
    )


def convert(client, size=5_000):
    return client.post(
        "/api/convert",
        files={"audio": ("speech.webm", b"y" * size, "audio/webm")},
    )
