import asyncio
import base64
import hashlib
import os
import secrets
import sqlite3
import time
from io import BytesIO
from pathlib import Path

import stripe
from dotenv import load_dotenv
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from openai import OpenAI
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

missing_keys = [
    name for name in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "SECRET_KEY")
    if not (os.getenv(name) or "").strip()
]
if missing_keys:
    print()
    print("=" * 60)
    for name in missing_keys:
        print(f"  PROBLEM: {name} is missing.")
    print()
    print("  Open the .env file in this folder and make sure each")
    print("  key is filled in.")
    print("=" * 60)
    print()
    input("Press Enter to close...")
    raise SystemExit(1)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# On Railway, DB_DIR points at the permanent disk; locally the app folder is used
DB_PATH = Path(os.getenv("DB_DIR", str(BASE_DIR))) / "users.db"
RECORDINGS_DIR = Path(os.getenv("DB_DIR", str(BASE_DIR))) / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_KEEP = 10
FAIR_USE_LIMIT = int(os.getenv("FAIR_USE_LIMIT", "300"))  # subscriber conversions per calendar month
IS_PRODUCTION = bool(os.getenv("RAILWAY_ENVIRONMENT"))

SITE_URL = os.getenv("SITE_URL", "https://myaccentchanger.com")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
FREE_CONVERSIONS = 2

# Anti-abuse settings
MAX_CLONE_BYTES = 14_000_000        # up to ~2 min voice sample; blocks giant uploads
MAX_CONVERT_BYTES = 4_000_000       # ~60s of speech; caps per-click AI cost
MAX_TRIALS_PER_IP_PER_DAY = 2       # 3rd+ signup from same IP in 24h gets no free tries
VOICE_CLEANUP_AFTER_DAYS = 3        # delete voice clones of expired trials after this many days
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
    "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org",
    "tempmail.dev", "yopmail.com", "trashmail.com", "getnada.com", "maildrop.cc",
    "dispostable.com", "fakeinbox.com", "mintemail.com", "throwawaymail.com",
    "mailnesia.com", "mytemp.email", "burnermail.io", "spamgourmet.com",
    "mohmal.com", "tempinbox.com", "emailondeck.com", "mail-temp.com",
}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                voice_id TEXT NOT NULL DEFAULT '',
                conversions_used INTEGER NOT NULL DEFAULT 0,
                is_subscribed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                signup_ip TEXT NOT NULL DEFAULT '',
                trial_limit INTEGER NOT NULL DEFAULT 2,
                trial_exhausted_at TEXT NOT NULL DEFAULT '',
                stripe_customer_id TEXT NOT NULL DEFAULT '',
                fair_month TEXT NOT NULL DEFAULT '',
                fair_used INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # add new columns if the database was created by an older version
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("signup_ip", "TEXT NOT NULL DEFAULT ''"),
            ("trial_limit", "INTEGER NOT NULL DEFAULT 2"),
            ("trial_exhausted_at", "TEXT NOT NULL DEFAULT ''"),
            ("stripe_customer_id", "TEXT NOT NULL DEFAULT ''"),
            ("fair_month", "TEXT NOT NULL DEFAULT ''"),
            ("fair_used", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=16384, r=8, p=1,
    ).hex()


app = FastAPI(title="Accent Changer")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY"), https_only=IS_PRODUCTION)
init_db()


def cleanup_expired_trial_voices():
    """Free up ElevenLabs voice slots held by trials that ended days ago."""
    with db() as conn:
        expired = conn.execute(
            "SELECT id, voice_id FROM users WHERE voice_id != '' AND is_subscribed = 0 "
            "AND trial_exhausted_at != '' "
            f"AND trial_exhausted_at <= datetime('now', '-{VOICE_CLEANUP_AFTER_DAYS} day')"
        ).fetchall()
    for row in expired:
        try:
            el_client.voices.delete(row["voice_id"])
        except Exception as e:
            print(f"Voice cleanup: could not delete {row['voice_id']}: {e}")
            continue
        with db() as conn:
            conn.execute("UPDATE users SET voice_id = '' WHERE id = ?", (row["id"],))
        print(f"Voice cleanup: freed slot for user {row['id']}")


async def cleanup_loop():
    while True:
        try:
            cleanup_expired_trial_voices()
        except Exception as e:
            print(f"Voice cleanup error: {e}")
        await asyncio.sleep(6 * 60 * 60)  # every 6 hours


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(cleanup_loop())


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def require_user(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Please log in first.")
    return user


def me_payload(user) -> dict:
    if user is None:
        return {"logged_in": False}
    left = max(0, user["trial_limit"] - user["conversions_used"])
    return {
        "logged_in": True,
        "email": user["email"],
        "has_voice": bool(user["voice_id"]),
        "subscribed": bool(user["is_subscribed"]),
        "free_conversions_left": left,
    }


def trial_over(user) -> bool:
    return not user["is_subscribed"] and user["conversions_used"] >= user["trial_limit"]


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/privacy")
def privacy():
    return FileResponse(BASE_DIR / "static" / "privacy.html")


@app.get("/terms")
def terms():
    return FileResponse(BASE_DIR / "static" / "terms.html")


@app.get("/api/me")
def me(request: Request):
    return me_payload(current_user(request))


@app.post("/api/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "That doesn't look like an email address.")
    if email.split("@")[-1] in DISPOSABLE_EMAIL_DOMAINS:
        raise HTTPException(400, "Please use your regular email address — temporary email services are not supported.")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    ip = client_ip(request)
    salt = secrets.token_hex(16)
    try:
        with db() as conn:
            recent_from_ip = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE signup_ip = ? AND signup_ip != '' "
                "AND created_at > datetime('now', '-1 day')",
                (ip,),
            ).fetchone()["n"]
            trial = 0 if recent_from_ip >= MAX_TRIALS_PER_IP_PER_DAY else FREE_CONVERSIONS
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, salt, signup_ip, trial_limit) VALUES (?, ?, ?, ?, ?)",
                (email, hash_password(password, salt), salt, ip, trial),
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(400, "An account with this email already exists. Try logging in.")
    request.session["user_id"] = user_id
    return me_payload(current_user(request))


@app.post("/api/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not secrets.compare_digest(
        user["password_hash"], hash_password(password, user["salt"])
    ):
        raise HTTPException(400, "Wrong email or password.")
    request.session["user_id"] = user["id"]
    return me_payload(user)


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"logged_in": False}


def require_admin(request: Request):
    user = current_user(request)
    if user is None or user["email"] not in ADMIN_EMAILS:
        raise HTTPException(404, "Not found.")  # pretend the page doesn't exist
    return user


@app.get("/admin")
def admin_page(request: Request):
    require_admin(request)
    return FileResponse(BASE_DIR / "static" / "admin.html")


@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, email, created_at, conversions_used, trial_limit, is_subscribed, "
            "voice_id != '' AS has_voice, signup_ip FROM users ORDER BY id DESC"
        ).fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/reset-trial")
def admin_reset_trial(request: Request, user_id: int = Form(...)):
    require_admin(request)
    with db() as conn:
        conn.execute(
            "UPDATE users SET conversions_used = 0, trial_exhausted_at = '', "
            "trial_limit = ? WHERE id = ?",
            (FREE_CONVERSIONS, user_id),
        )
    return {"ok": True}


@app.post("/api/admin/set-password")
def admin_set_password(request: Request, user_id: int = Form(...)):
    require_admin(request)
    new_password = secrets.token_urlsafe(9)
    salt = secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (hash_password(new_password, salt), salt, user_id),
        )
    return {"password": new_password}


@app.post("/api/admin/toggle-sub")
def admin_toggle_sub(request: Request, user_id: int = Form(...)):
    require_admin(request)
    with db() as conn:
        conn.execute(
            "UPDATE users SET is_subscribed = 1 - is_subscribed, trial_exhausted_at = '' WHERE id = ?",
            (user_id,),
        )
    return {"ok": True}


@app.get("/api/history")
def history(request: Request):
    user = require_user(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, transcript, created_at FROM conversions WHERE user_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/recording/{conv_id}")
def recording(conv_id: int, request: Request):
    user = require_user(request)
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM conversions WHERE id = ? AND user_id = ?",
            (conv_id, user["id"]),
        ).fetchone()
    path = RECORDINGS_DIR / f"{conv_id}.mp3"
    if row is None or not path.exists():
        raise HTTPException(404, "Recording not found.")
    return FileResponse(path, media_type="audio/mpeg", filename=f"accent-changer-{conv_id}.mp3")


@app.post("/api/checkout")
def checkout(request: Request):
    user = require_user(request)
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        raise HTTPException(503, "Payments aren't switched on yet — please try again later.")
    if user["is_subscribed"]:
        raise HTTPException(400, "You already have an active subscription.")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            customer_email=user["email"],
            client_reference_id=str(user["id"]),
            success_url=f"{SITE_URL}/?paid=1",
            cancel_url=f"{SITE_URL}/",
        )
    except Exception as e:
        print(f"Checkout error for user {user['id']}: {e}")
        raise HTTPException(502, "Could not open the payment page — please try again in a minute.")
    return {"url": session.url}


@app.post("/api/portal")
def portal(request: Request):
    user = require_user(request)
    if not user["stripe_customer_id"]:
        raise HTTPException(400, "No subscription found for this account. If that seems wrong, email support.")
    try:
        session = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=SITE_URL,
        )
    except Exception as e:
        print(f"Portal error for user {user['id']}: {e}")
        raise HTTPException(502, "Could not open the subscription page — please try again, or email support.")
    return {"url": session.url}


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Invalid signature")

    kind = event["type"]
    obj = event["data"]["object"]

    if kind == "checkout.session.completed":
        user_id = int(obj.get("client_reference_id") or 0)
        customer_id = obj.get("customer") or ""
        if user_id:
            with db() as conn:
                conn.execute(
                    "UPDATE users SET is_subscribed = 1, stripe_customer_id = ?, trial_exhausted_at = '' WHERE id = ?",
                    (customer_id, user_id),
                )
            print(f"Webhook: user {user_id} subscribed")
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer") or ""
        active = kind == "customer.subscription.updated" and obj.get("status") in ("active", "trialing")
        if customer_id:
            with db() as conn:
                conn.execute(
                    "UPDATE users SET is_subscribed = ? WHERE stripe_customer_id = ?",
                    (1 if active else 0, customer_id),
                )
            print(f"Webhook: customer {customer_id} active={active}")

    return {"received": True}


@app.post("/api/clone")
async def clone(request: Request, audio: UploadFile = File(...), consent: str = Form("")):
    user = require_user(request)
    if trial_over(user):
        raise HTTPException(402, "Your free trial is finished — subscribe below to keep going.")
    if consent != "yes":
        raise HTTPException(400, "Please confirm that this is your own voice.")
    data = await audio.read()
    if len(data) < 150_000:
        raise HTTPException(400, "That recording is too short. Please record at least 1 minute.")
    if len(data) > MAX_CLONE_BYTES:
        raise HTTPException(400, "That recording is too long. Please keep it under 2 minutes.")

    sample = BytesIO(data)
    sample.name = audio.filename or "sample.webm"
    try:
        voice = el_client.voices.ivc.create(
            name=f"AccentChanger {user['email']}",
            files=[sample],
            description="Voice clone for accent correction",
        )
    except Exception as e:
        print(f"Clone error for user {user['id']}: {e}")
        raise HTTPException(502, "We couldn't clone your voice just now — please try again in a minute. If it keeps happening, email support and we'll sort it out.")

    # free the slot held by this user's previous clone, if any
    if user["voice_id"]:
        try:
            el_client.voices.delete(user["voice_id"])
        except Exception as e:
            print(f"Re-clone: could not delete old voice {user['voice_id']}: {e}")

    with db() as conn:
        conn.execute("UPDATE users SET voice_id = ? WHERE id = ?", (voice.voice_id, user["id"]))
    return me_payload(current_user(request))


@app.post("/api/convert")
async def convert(request: Request, audio: UploadFile = File(...)):
    user = require_user(request)
    if not user["voice_id"]:
        raise HTTPException(400, "Clone your voice first (Step 1).")
    if trial_over(user):
        raise HTTPException(402, "Your free trial is finished — subscribe below to keep going.")

    this_month = time.strftime("%Y-%m", time.gmtime())
    fair_used = user["fair_used"] if user["fair_month"] == this_month else 0
    if user["is_subscribed"] and fair_used >= FAIR_USE_LIMIT:
        raise HTTPException(
            429,
            "You've reached this month's fair-use limit of 300 conversions. It resets on the 1st of next month.",
        )

    data = await audio.read()
    if len(data) < 2_000:
        raise HTTPException(400, "The recording was empty. Try again.")
    if len(data) > MAX_CONVERT_BYTES:
        raise HTTPException(400, "That recording is too long. Please keep it under 60 seconds.")

    try:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(audio.filename or "speech.webm", data),
            language="en",
        )
    except Exception as e:
        print(f"Transcription error for user {user['id']}: {e}")
        raise HTTPException(502, "We couldn't process that recording — please try again in a minute.")

    text = transcript.text.strip()
    if not text:
        raise HTTPException(400, "Could not hear anything. Try again.")

    try:
        audio_chunks = el_client.text_to_speech.convert(
            voice_id=user["voice_id"],
            text=text,
            model_id=os.getenv("TTS_MODEL", "eleven_multilingual_v2"),
            voice_settings=VoiceSettings(
                stability=float(os.getenv("TTS_STABILITY", "0.75")),
                similarity_boost=float(os.getenv("TTS_SIMILARITY", "0.95")),
                style=0.0,
                use_speaker_boost=True,
            ),
        )
        mp3 = b"".join(audio_chunks)
    except Exception as e:
        print(f"TTS error for user {user['id']}: {e}")
        raise HTTPException(502, "We're a bit busy right now — your recording wasn't counted. Please try again in a minute.")

    with db() as conn:
        conn.execute(
            "UPDATE users SET conversions_used = conversions_used + 1 WHERE id = ?",
            (user["id"],),
        )
        conn.execute(
            "UPDATE users SET trial_exhausted_at = datetime('now') "
            "WHERE id = ? AND is_subscribed = 0 AND conversions_used >= trial_limit "
            "AND trial_exhausted_at = ''",
            (user["id"],),
        )
        conn.execute(
            "UPDATE users SET fair_month = ?, fair_used = ? WHERE id = ?",
            (this_month, fair_used + 1, user["id"]),
        )
        cur = conn.execute(
            "INSERT INTO conversions (user_id, transcript) VALUES (?, ?)",
            (user["id"], text),
        )
        conv_id = cur.lastrowid
        old_rows = conn.execute(
            "SELECT id FROM conversions WHERE user_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
            (user["id"], HISTORY_KEEP),
        ).fetchall()
        for row in old_rows:
            conn.execute("DELETE FROM conversions WHERE id = ?", (row["id"],))
    (RECORDINGS_DIR / f"{conv_id}.mp3").write_bytes(mp3)
    for row in old_rows:
        (RECORDINGS_DIR / f"{row['id']}.mp3").unlink(missing_ok=True)

    return JSONResponse({
        "transcript": text,
        "audio_b64": base64.b64encode(mp3).decode("ascii"),
        "me": me_payload(current_user(request)),
    })


if __name__ == "__main__":
    import uvicorn
    if IS_PRODUCTION:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), proxy_headers=True)
    else:
        uvicorn.run(app, host="127.0.0.1", port=8000)
