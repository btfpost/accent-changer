import time

import server
from conftest import signup, clone, convert


# ---------- accounts ----------

def test_signup_gives_two_free_conversions(client):
    r = signup(client)
    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is True
    assert body["free_conversions_left"] == 2
    assert body["has_voice"] is False


def test_signup_rejects_bad_email(client):
    assert signup(client, email="not-an-email").status_code == 400


def test_signup_rejects_short_password(client):
    assert signup(client, password="short").status_code == 400


def test_signup_rejects_disposable_email(client):
    r = signup(client, email="x@mailinator.com")
    assert r.status_code == 400
    assert "temporary email" in r.json()["detail"]


def test_signup_rejects_duplicate_email(client):
    assert signup(client).status_code == 200
    assert signup(client).status_code == 400


def test_login_and_logout(client):
    signup(client)
    client.post("/api/logout")
    assert client.get("/api/me").json()["logged_in"] is False

    bad = client.post("/api/login", data={"email": "user@test.com", "password": "wrongwrong"})
    assert bad.status_code == 400

    good = client.post("/api/login", data={"email": "user@test.com", "password": "password123"})
    assert good.status_code == 200
    assert client.get("/api/me").json()["logged_in"] is True


# ---------- anti-abuse ----------

def test_third_signup_from_same_ip_gets_no_trial(client):
    assert signup(client, email="a@test.com", ip="9.9.9.9").json()["free_conversions_left"] == 2
    client.post("/api/logout")
    assert signup(client, email="b@test.com", ip="9.9.9.9").json()["free_conversions_left"] == 2
    client.post("/api/logout")
    assert signup(client, email="c@test.com", ip="9.9.9.9").json()["free_conversions_left"] == 0
    client.post("/api/logout")
    # a different household is unaffected
    assert signup(client, email="d@test.com", ip="8.8.8.8").json()["free_conversions_left"] == 2


# ---------- voice cloning ----------

def test_clone_requires_consent(client, fake_voice_apis):
    signup(client)
    assert clone(client, consent="").status_code == 400


def test_clone_rejects_too_short_and_too_long(client, fake_voice_apis):
    signup(client)
    assert clone(client, size=10_000).status_code == 400          # under ~1 minute
    assert clone(client, size=15_000_000).status_code == 400      # over ~2 minutes


def test_clone_success_and_reclone_frees_old_slot(client, fake_voice_apis):
    signup(client)
    r = clone(client)
    assert r.status_code == 200
    assert r.json()["has_voice"] is True
    # re-cloning must delete the previous ElevenLabs voice
    clone(client)
    assert fake_voice_apis["deleted_voices"] == ["fake-voice-id"]


# ---------- conversion ----------

def test_convert_requires_voice(client, fake_voice_apis):
    signup(client)
    r = convert(client)
    assert r.status_code == 400


def test_convert_success_counts_trial_and_saves_history(client, fake_voice_apis):
    signup(client)
    clone(client)
    r = convert(client)
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "Hello world"
    assert body["audio_b64"]
    assert body["me"]["free_conversions_left"] == 1

    items = client.get("/api/history").json()["items"]
    assert len(items) == 1
    rec = client.get(f"/api/recording/{items[0]['id']}")
    assert rec.status_code == 200
    assert rec.content == b"fake-mp3-bytes"


def test_trial_runs_out_after_two_conversions(client, fake_voice_apis):
    signup(client)
    clone(client)
    assert convert(client).status_code == 200
    assert convert(client).status_code == 200
    r = convert(client)
    assert r.status_code == 402
    # cloning is also blocked once the trial is over
    assert clone(client).status_code == 402


def test_users_cannot_read_others_recordings(client, fake_voice_apis):
    signup(client, email="a@test.com")
    clone(client)
    convert(client)
    rec_id = client.get("/api/history").json()["items"][0]["id"]
    client.post("/api/logout")
    signup(client, email="b@test.com")
    assert client.get(f"/api/recording/{rec_id}").status_code == 404


def test_subscriber_hits_fair_use_cap(client, fake_voice_apis):
    signup(client)
    clone(client)
    this_month = time.strftime("%Y-%m", time.gmtime())
    with server.db() as conn:
        conn.execute(
            "UPDATE users SET is_subscribed = 1, fair_month = ?, fair_used = ?",
            (this_month, server.FAIR_USE_LIMIT),
        )
    r = convert(client)
    assert r.status_code == 429


# ---------- payments ----------

def test_checkout_disabled_without_stripe_keys(client):
    signup(client)
    assert client.post("/api/checkout").status_code == 503


def test_webhook_rejects_unsigned_calls(client):
    r = client.post("/api/stripe-webhook", content=b"{}")
    assert r.status_code == 400


# ---------- admin ----------

def test_admin_pages_hidden_from_regular_users(client):
    assert client.get("/api/admin/users").status_code == 404  # anonymous
    signup(client, email="regular@test.com")
    assert client.get("/api/admin/users").status_code == 404  # logged in, not admin
    assert client.get("/admin").status_code == 404


def test_admin_can_list_and_reset(client, fake_voice_apis):
    signup(client, email="customer@test.com", ip="7.7.7.7")
    clone(client)
    convert(client)
    convert(client)
    client.post("/api/logout")

    signup(client, email="admin@test.com", ip="6.6.6.6")
    users = client.get("/api/admin/users").json()["users"]
    customer = next(u for u in users if u["email"] == "customer@test.com")
    assert customer["conversions_used"] == 2

    client.post("/api/admin/reset-trial", data={"user_id": customer["id"]})
    users = client.get("/api/admin/users").json()["users"]
    customer = next(u for u in users if u["email"] == "customer@test.com")
    assert customer["conversions_used"] == 0

    out = client.post("/api/admin/set-password", data={"user_id": customer["id"]}).json()
    client.post("/api/logout")
    r = client.post("/api/login", data={"email": "customer@test.com", "password": out["password"]})
    assert r.status_code == 200


# ---------- pages ----------

def test_public_pages_load(client):
    for path in ("/", "/privacy", "/terms"):
        assert client.get(path).status_code == 200
