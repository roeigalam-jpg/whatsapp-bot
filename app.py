from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from datetime import datetime, timezone, timedelta
import base64
import hashlib
import hmac
import requests
import json
import threading
import time
import os

app = Flask(__name__)

# ─── הגדרות ───────────────────────────────────────────────────
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not FLASK_SECRET_KEY:
    FLASK_SECRET_KEY = os.urandom(24)
    print("[Auth] FLASK_SECRET_KEY לא הוגדר — נוצר מפתח אקראי.", flush=True)
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on"):
    app.config["SESSION_COOKIE_SECURE"] = True

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_TOKEN    = os.environ.get("ADMIN_TOKEN", "").strip()
AUTH_CONFIGURED = bool(ADMIN_PASSWORD or ADMIN_TOKEN)

GREEN_API_INSTANCE = os.environ.get("GREEN_API_INSTANCE", "").strip()
GREEN_API_TOKEN    = os.environ.get("GREEN_API_TOKEN", "").strip()
GREEN_API_HOST     = os.environ.get("GREEN_API_HOST", "https://api.green-api.com").rstrip("/")
GREEN_API_URL      = f"{GREEN_API_HOST}/waInstance{GREEN_API_INSTANCE}" if GREEN_API_INSTANCE else ""

# ─── ניתוב קריאות ────────────────────────────────────────────
NOTIFY_PERSONAL_PHONE = os.environ.get("NOTIFY_PERSONAL_PHONE", "972527066110").strip()
NOTIFY_GROUP_ID       = os.environ.get("NOTIFY_GROUP_ID", "972529532110-1614167768@g.us").strip()
notify_to_group = False

BOSS_PHONE      = os.environ.get("BOSS_PHONE", "0502580803").strip()
BUSINESS_NAME   = os.environ.get("BUSINESS_NAME", "שירות לקוחות").strip()
BOT_NAME        = os.environ.get("BOT_NAME", "גל").strip()
BUSINESS_DOMAIN = os.environ.get("BUSINESS_DOMAIN", "בריכות שחייה").strip()
PORTAL_ACCENT   = os.environ.get("PORTAL_COLOR_ACCENT", "#25d366").strip()
PORTAL_BG       = os.environ.get("PORTAL_COLOR_BG", "#0b0d12").strip()
KEEP_ALIVE_URL = os.environ.get("KEEP_ALIVE_URL", "").strip()
ENABLE_KEEP_ALIVE = os.environ.get("ENABLE_KEEP_ALIVE", "false").strip().lower() in ("1", "true", "yes", "on")
USE_POLLING    = os.environ.get("USE_POLLING", "true").strip().lower() in ("1", "true", "yes", "on")

# ─── AUTO_BOT כבוי — רק הפעלה ידנית ─────────────────────────
AUTO_BOT_NEW_CHATS = False

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
FLASK_DEBUG    = os.environ.get("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")
_raw_port = (os.environ.get("PORT") or os.environ.get("FLASK_PORT") or "5000").strip()
try:
    FLASK_PORT = int(_raw_port)
except ValueError:
    FLASK_PORT = 5000

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-20250514"
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM    = "onboarding@resend.dev"
WIZENET_API_TOKEN = os.environ.get("WIZENET_API_TOKEN", "").strip()
WIZENET_URL       = os.environ.get("WIZENET_URL", "").strip()
WIZENET_BASE_URL  = "https://aquapoolco.wizenet.co.il/Wizeapi"

# ─── Firebase Firestore ───────────────────────────────────────
FIREBASE_PROJECT_ID  = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
FIRESTORE_DOC        = "bot-data/state"

_db = None

def _get_db():
    global _db
    if _db is not None:
        return _db
    if not FIREBASE_PROJECT_ID or not FIREBASE_CREDENTIALS:
        return None
    try:
        import google.auth
        from google.oauth2 import service_account
        from google.cloud import firestore
        creds_dict = json.loads(FIREBASE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _db = firestore.Client(project=FIREBASE_PROJECT_ID, credentials=creds)
        print("[Firestore] connected", flush=True)
    except Exception as e:
        print(f"[Firestore] init error: {e}", flush=True)
        _db = None
    return _db

state_lock  = threading.RLock()
_seen_event_keys = {}
_seen_lock  = threading.Lock()
MAX_SEEN_KEYS = 8000

# ─── נתונים ───────────────────────────────────────────────────
sessions      = {}
service_calls = []
bot_enabled   = {}
chat_history  = {}
greeting_sent = {}
global_bot_on = True
notify_to_group_state = False
runtime_settings = {
    "notify_personal_phone": "972527066110",
    "notify_group_id": "972529532110-1614167768@g.us",
    "boss_phone": "0502580803",
    "webhook_url": "",
    "webhook_headers": "",
    "notification_emails": []
}
last_bot_msg_time = {}
reminder_timers   = {}
processing_phones = {}   # phone → timestamp התחלת עיבוד
pending_messages   = {}  # phone → (body_text, msg_type, audio_url) — הודעה שהגיעה בזמן עיבוד
PROCESSING_TIMEOUT = 60  # שניות מקסימום לנעילה
# קריאות שממתינות לאישור לקוח — phone → call_data
pending_wizenet_confirm = {}  # מספרים שנמצאים בעיבוד כרגע

# ─── Firestore שמירה/טעינה ────────────────────────────────────
_last_save_ts = 0  # timestamp של השמירה האחרונה
_data_loaded = False  # האם כבר נטענו נתונים בהפעלה

def _save_firestore(payload):
    """שמירה ל-Firestore — סינכרונית"""
    db = _get_db()
    if db:
        try:
            col, doc = FIRESTORE_DOC.split("/")
            db.collection(col).document(doc).set(payload)
            print(f"[Firestore] saved ok", flush=True)
        except Exception as e:
            print(f"[Firestore] save error: {e}", flush=True)

def save_data(sync_firestore=False):
    global _last_save_ts
    now_ts = time.time()
    _last_save_ts = now_ts
    with state_lock:
        payload = {
            "sessions": sessions,
            "service_calls": service_calls,
            "bot_enabled": bot_enabled,
            "chat_history": chat_history,
            "greeting_sent": greeting_sent,
            "global_bot_on": global_bot_on,
            "notify_to_group": notify_to_group_state,
            "runtime_settings": runtime_settings,
            "_save_ts": now_ts
        }
    # שמור על disk בלבד — Firestore לא בשימוש בזמן ריצה
    try:
        import os as _os
        _os.makedirs("/data", exist_ok=True)
        with open("/data/data.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Save] error: {e}", flush=True)

def load_data():
    global sessions, service_calls, bot_enabled, chat_history, greeting_sent, global_bot_on, notify_to_group_state, _data_loaded
    if _data_loaded:
        return
    loaded = None

    # טען מ-disk בלבד
    for path in ["/data/data.json", "data.json"]:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                print(f"[Load] נטען מ-{path}", flush=True)
                break
        except Exception as e:
            print(f"[Load] error from {path}: {e}", flush=True)

    # fallback — נסה Firestore רק אם אין קובץ מקומי
    if not loaded:
        db = _get_db()
        if db:
            try:
                col, doc = FIRESTORE_DOC.split("/")
                snap = db.collection(col).document(doc).get()
                if snap.exists:
                    loaded = snap.to_dict()
                    print("[Load] Firestore fallback", flush=True)
            except Exception as e:
                print(f"[Firestore] load error: {e}", flush=True)
    if loaded:
        with state_lock:
            sessions           = loaded.get("sessions", {})
            service_calls      = loaded.get("service_calls", [])
            bot_enabled        = loaded.get("bot_enabled", {})
            chat_history       = loaded.get("chat_history", {})
            greeting_sent      = loaded.get("greeting_sent", {})
            global_bot_on      = loaded.get("global_bot_on", True)
            notify_to_group_state = loaded.get("notify_to_group", False)
            if loaded.get("runtime_settings"):
                runtime_settings.update(loaded["runtime_settings"])
    _data_loaded = True

load_data()

# ─── כלים ─────────────────────────────────────────────────────
def il_now():
    """זמן ישראל נכון — UTC+3"""
    return datetime.now(timezone.utc) + timedelta(hours=3)

def phone972(p):
    p = str(p).replace("@c.us", "").replace("-", "").replace(" ", "").strip()
    if not p:
        return ""
    if p.startswith("0"):
        p = "972" + p[1:]
    if not p.startswith("972"):
        p = "972" + p
    return p

def is_boss_phone(phone):
    if not BOSS_PHONE:
        return False
    return phone972(phone) == phone972(BOSS_PHONE)

def get_greeting():
    h = il_now().hour
    if 5 <= h < 12:
        return "בוקר טוב"
    if 12 <= h < 17:
        return "צהריים טובים"
    if 17 <= h < 22:
        return "ערב טוב"
    return "לילה טוב"

def validate_il_phone(p):
    """בדיקת מספר ישראלי — 05X + 7 ספרות"""
    clean = str(p).replace("-", "").replace(" ", "").replace("+", "")
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    if len(clean) != 10:
        return False
    if not clean.startswith("05"):
        return False
    return clean.isdigit()

def normalize_il_phone(p):
    """נרמול מספר טלפון לפורמט 05X"""
    clean = str(p).replace("-", "").replace(" ", "").replace("+", "")
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    return clean

def validate_address_basic(address):
    """בדיקת היגיון בסיסי בכתובת"""
    if not address or len(address.strip()) < 5:
        return False, "כתובת קצרה מדי"
    nonsense = ["כוכב הבא", "כוכב", "test", "טסט", "אבגד", "xxxx", "1234", "asdf"]
    addr_lower = address.lower()
    for n in nonsense:
        if n in addr_lower:
            return False, "כתובת לא תקינה"
    # חייב להכיל לפחות מספר אחד (מספר בית) ואות אחת
    has_digit = any(c.isdigit() for c in address)
    has_letter = any(c.isalpha() for c in address)
    if not (has_digit and has_letter):
        return False, "נא לכלול שם רחוב ומספר בית"
    return True, ""

def admin_authenticated():
    if not AUTH_CONFIGURED:
        return True
    if ADMIN_TOKEN and request.headers.get("Authorization", "") == f"Bearer {ADMIN_TOKEN}":
        return True
    return session.get("admin") is True

@app.before_request
def _require_admin():
    if request.endpoint in ("webhook", "google_callback", "login_page", "health_check", "ping"):
        return
    if request.endpoint is None:
        return
    if admin_authenticated():
        return
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "לא מאומת"}), 401
    nxt = request.path
    if request.query_string:
        nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
    return redirect(url_for("login_page", next=nxt))

def extract_message_id(payload):
    if not isinstance(payload, dict):
        return None
    # נסה כל המיקומים האפשריים של message ID ב-Green API
    for key in ("idMessage", "messageId"):
        v = payload.get(key)
        if v:
            return str(v)
    # תוך messageData
    md = payload.get("messageData") or {}
    for key in ("idMessage", "messageId"):
        v = md.get(key)
        if v:
            return str(v)
    # תוך senderData (לפעמים)
    sd = payload.get("senderData") or {}
    v = sd.get("idMessage")
    if v:
        return str(v)
    # fallback — שלב sender + timestamp כ-unique key
    sender = (payload.get("senderData") or {}).get("chatId", "")
    ts = payload.get("timestamp", "")
    if sender and ts:
        return f"{sender}:{ts}"
    return None

def is_duplicate_green_event(body, receipt_id):
    mid = extract_message_id(body)
    print(f"[Dedup] mid={mid} receipt={receipt_id} keys_in_body={list(body.keys()) if isinstance(body,dict) else []}", flush=True)
    if not mid:
        # אם אין ID — השתמש ב-receipt_id
        if receipt_id:
            mid = f"r:{receipt_id}"
        else:
            return False
    key = f"m:{mid}"
    now = time.monotonic()
    with _seen_lock:
        if key in _seen_event_keys:
            print(f"[Dedup] כפילות חסומה: {key}", flush=True)
            return True
        _seen_event_keys[key] = now
        if len(_seen_event_keys) > MAX_SEEN_KEYS:
            for k, _ in sorted(_seen_event_keys.items(), key=lambda x: x[1])[:3000]:
                del _seen_event_keys[k]
    return False

# ─── System Prompts ───────────────────────────────────────────
def build_system_prompt(phone=""):
    greeting = get_greeting()
    phone_display = phone972(phone).replace("972", "0", 1) if phone else ""
    phone_hint = f"\nמספר הוואטסאפ של הלקוח: {phone_display}" if phone_display else ""
    phone_note = f"טלפון: יש לך את מספר הוואטסאפ {phone_display} — שאל 'האם להשתמש בו?' ואם אישר, השתמש בו. אל תשאל שוב." if phone_display else "טלפון: נסה לקבל, אם הלקוח לא נותן — דלג וסכם בלעדיו."
    return f"""אתה {BOT_NAME} — העוזר של רועי, מומחה בריכות שחייה.{phone_hint}

זהות:
- שמך {BOT_NAME}, אם שואלים: "אני {BOT_NAME}, העוזר של רועי"
- אל תציין שם חברה, אל תחשוף פרטים על רועי — "רועי ייצור קשר בהקדם"

שעה נוכחית בישראל: {il_now().strftime('%H:%M')} — ברכה: "{greeting}"

שירותים שאנו מציעים:
1. בנייה, אבזור ועבודות גמר
2. תכנון ועיצוב בריכות
3. תחזוקה שוטפת ותקלות
4. שיפוץ וחידוש בריכות

אם הבקשה לא מתחום הבריכות — "אנחנו מתמחים בבריכות בלבד"

סגנון — חם, קצר, יעיל:
- משפט אחד-שניים בלבד
- זהה מין מהשם
- אמפתיה קצרה בלבד ("לא נעים, נטפל")
- אין שמות עובדים

חוקי ברזל:
1. שמור כל מידע שהלקוח נתן — אל תשאל עליו שוב לעולם
2. אסוף הכל בשאלה אחת אם אפשר: "מה שמך, כתובת הבריכה ומה הבעיה?"
3. אם יש לך שם + כתובת + תיאור — עבור לסיכום מיד, אל תחכה לטלפון
4. {phone_note}
5. אחרי 3 הודעות ללא התקדמות — הצע תפריט:
   "במה אפשר לעזור?
   1️⃣ בנייה / אבזור
   2️⃣ תכנון ועיצוב
   3️⃣ תחזוקה / תקלה
   4️⃣ שיפוץ וחידוש"
6. תמונה שהתקבלה — שאל: "מה רואים בתמונה?"

כשיש שם + כתובת + תיאור — הצג סיכום ובקש אישור:
"סיכום: [שם], [כתובת], [תיאור]. נכון?"

אחרי אישור — JSON בדיוק:
{{"action":"open_call","name":"...","address":"...","call_type":"...","description":"...","contact_phone":"...","tech_name":""}}

call_type לפי הקשר:
- תחזוקה/מים/תקלה/ניטור → "תחזוקה"
- בנייה/פרויקט/אבזור → "פרויקט"
- שיפוץ/חידוש → "שיפוץ"
- חשמל → "חשמל"

ביטול: {{"action":"cancelled"}}
אחרת: {{"action":"continue","message":"..."}}

אל תציין מספר קריאה בשיחה.

אם הלקוח שלח משהו לא ברור (נקודה, אות, "?", "הי" וכד') — ענה בחמימות: "היי! איך אפשר לעזור?" ואל תתעלם.

חוק סגירת שיחה:
- אם הלקוח אמר שאין לו בריכה / לא רלוונטי / לא מעוניין / "קיביניאמאט" / קללה / ביטול — ענה בנימוס: "בסדר גמור! אם תצטרך עזרה עם הבריכה בעתיד — אנחנו כאן 😊" והחזר {{"action":"cancelled"}}
- אל תמשיך לשאול אחרי שהלקוח הביע חוסר עניין
- אחרי 2 תגובות שליליות ברצף — סגור את השיחה בנימוס"""

BOSS_SYSTEM_PROMPT = """אתה גל — העוזר האישי של רועי.
ישיר, קצר, יעיל. עברית טבעית.

יכולות: ענה על כל שאלה — בריכות, עסקים, טכנולוגיה, הכל.

חוקי ברזל:
1. שיחה כללית — ענה ישירות, אל תפתח קריאה
2. פתח קריאה רק כשיש שם לקוח + תיאור — אל תחכה לטלפון
3. אחרי "[קריאה נפתחה — שיחה חדשה]" — שכח הכל מלפני, שיחה חדשה
4. אל תשאל שוב על מידע שכבר קיבלת
5. אל תערב פרטים משיחות קודמות — כל קריאה עצמאית
6. שלח הודעה רק אם רועי מבקש עם מספר ותוכן

פתיחת קריאה — מיד כשיש שם + תיאור:
{"action":"open_call","name":"...","address":"...","call_type":"...","description":"...","contact_phone":"-","tech_name":""}

call_type: תחזוקה/מים/תקלה→"תחזוקה" | בנייה/פרויקט→"פרויקט" | שיפוץ→"שיפוץ" | חשמל→"חשמל"

שליחת הודעה:
{"action":"send_message","phone":"...","message":"..."}

שיחה רגילה:
{"action":"continue","message":"תשובה קצרה"}"""

def parse_green_msg(msg_data):
    msg_type_raw = (msg_data or {}).get("typeMessage", "textMessage")
    type_map = {
        "textMessage":         ("text",     lambda d: d.get("textMessageData",{}).get("textMessage","") or d.get("extendedTextMessageData",{}).get("text","")),
        "extendedTextMessage": ("text",     lambda d: d.get("extendedTextMessageData",{}).get("text","") or d.get("textMessageData",{}).get("textMessage","")),
        "imageMessage":        ("image",    lambda d: "[שלח תמונה]"),
        "audioMessage":        ("audio",    lambda d: "[שלח הקלטה קולית]"),
        "videoMessage":        ("video",    lambda d: "[שלח וידאו]"),
        "callMessage":         ("text",     lambda d: "[שיחת וואטסאפ]"),
        "documentMessage":     ("document", lambda d: "[שלח מסמך]"),
        "stickerMessage":      ("sticker",  lambda d: "[שלח סטיקר]"),
        "locationMessage":     ("text",     lambda d: "[שיתף מיקום]"),
        "contactMessage":      ("text",     lambda d: "[שיתף איש קשר]"),
    }
    msg_type, extractor = type_map.get(msg_type_raw, ("text", lambda d: ""))
    return msg_type, extractor(msg_data) or ""

def extract_audio_url(msg_data):
    if not msg_data:
        return None
    for key in ("audioMessageData", "fileMessageData", "documentMessageData"):
        block = msg_data.get(key) or {}
        u = block.get("downloadUrl") or block.get("url") or block.get("directPath")
        if u and str(u).startswith("http"):
            return str(u)
    return None

def send_message(phone, text):
    if not GREEN_API_URL or not GREEN_API_TOKEN:
        print("[GreenAPI] לא מוגדר", flush=True)
        return False
    try:
        url = f"{GREEN_API_URL}/sendMessage/{GREEN_API_TOKEN}"
        chat_id = phone if "@" in phone else f"{phone}@c.us"
        r = requests.post(url, json={"chatId": chat_id, "message": text}, timeout=10)
        ok = r.status_code == 200
        if not ok:
            print(f"[GreenAPI] send failed: {r.status_code} {r.text[:100]}", flush=True)
        return ok
    except Exception as e:
        print(f"[GreenAPI] error: {e}", flush=True)
        return False

def add_to_history(phone, sender, message, msg_type="text"):
    now = il_now()
    entry = {
        "sender": sender,
        "message": message,
        "time": now.strftime("%H:%M"),
        "type": msg_type,
        "ts": now.isoformat()
    }
    with state_lock:
        chat_history.setdefault(phone, []).append(entry)

def get_session(phone):
    with state_lock:
        if phone not in sessions:
            sessions[phone] = {"step": "active", "data": {}}
        return sessions[phone]

def reset_session(phone):
    with state_lock:
        sessions[phone] = {"step": "active", "data": {}}

def is_group(phone):
    return "@g.us" in str(phone)

def build_notify_message(phone, data):
    client_num = str(phone).replace("@c.us", "").replace("972", "0", 1)
    now_str = il_now().strftime("%d/%m/%Y %H:%M")
    wizenet_line = f"🗂 *קריאה בוויזנט:* #{data.get('wizenet_id')}" if data.get('wizenet_id') else "⚡ נא לפתוח קריאה במערכת."
    return "\n".join([
        "🔔 *קריאה חדשה*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 *שם:* {data.get('name','-')}",
        f"📞 *טלפון:* {data.get('contact_phone','-')}",
        f"📍 *כתובת:* {data.get('address','-')}",
        f"🔧 *סוג:* {data.get('call_type','-')}",
        f"📝 *תיאור:* {data.get('description','-')}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📱 *מספר לקוח:* {client_num}",
        f"🕐 *נפתח:* {now_str}",
        "",
        wizenet_line
    ])

def ask_claude(history, user_msg, msg_type="text", is_boss=False, phone=""):
    if not (ANTHROPIC_KEY or "").strip():
        return {"action": "continue", "message": "שירות הבוט לא פעיל כרגע. צור קשר ישיר."}
    
    # ניסיון עם retry
    for attempt in range(2):
        try:
            messages = []
            for h in history[-14:]:
                role = "user" if h["sender"] == "client" else "assistant"
                content = h["message"]
                if h.get("type") in ["image","audio","document","video","sticker"] and h["sender"] == "client":
                    content = f"[הלקוח שלח {h.get('type','קובץ')}] {content}"
                if messages and messages[-1]["role"] == role:
                    messages[-1]["content"] += f"\n{content}"
                else:
                    messages.append({"role": role, "content": content})

            current_msg = f"[הלקוח שלח {msg_type}] {user_msg}" if msg_type in ["image","audio","document","video","sticker"] else user_msg
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n{current_msg}"
            else:
                messages.append({"role": "user", "content": current_msg})

            system = BOSS_SYSTEM_PROMPT if is_boss else build_system_prompt(phone)
            resp = requests.post(
                CLAUDE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 800 if is_boss else 450,
                    "system": system,
                    "messages": messages
                },
                timeout=25
            )
            data = resp.json()
            if "content" not in data:
                print(f"[Claude] error: {data}", flush=True)
                if attempt == 0:
                    time.sleep(1)
                    continue
                return {"action": "continue", "message": "מצטער, לא הצלחתי להשיב. נסה שוב."}
            text = data["content"][0]["text"].strip()
            try:
                start = text.find("{")
                end   = text.rfind("}") + 1
                if start != -1 and end > start:
                    return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
            return {"action": "continue", "message": text}
        except Exception as e:
            print(f"[Claude] attempt {attempt+1} error: {e}", flush=True)
            if attempt == 0:
                time.sleep(1)
    return {"action": "continue", "message": "מצטער, שגיאה זמנית. נסה שוב בעוד רגע."}

def cancel_reminder(phone):
    """מבטל את כל הטיימרים הפעילים לאותו מספר"""
    with state_lock:
        timers = reminder_timers.pop(phone, [])
        cancel_flag = reminder_timers.pop(phone + "_cancel", None)
    # סמן ביטול — מונע תזכורת שניה גם אם הטיימר כבר התחיל
    if cancel_flag:
        cancel_flag.set()
    if isinstance(timers, list):
        for t in timers:
            t.cancel()
    elif timers:
        timers.cancel()

def schedule_reminder(phone, last_msg):
    # מבטל תזכורות קודמות
    cancel_reminder(phone)
    cancelled = threading.Event()  # flag לביטול

    def remind_first():
        if cancelled.is_set():
            return
        with state_lock:
            on = bot_enabled.get(phone, False) and global_bot_on
        if not on or cancelled.is_set():
            return
        send_message(phone, last_msg)
        add_to_history(phone, "bot", f"[תזכורת 1] {last_msg}")
        save_data()

        def remind_second():
            if cancelled.is_set():
                return
            with state_lock:
                on2 = bot_enabled.get(phone, False) and global_bot_on
            if not on2 or cancelled.is_set():
                return
            msg2 = "רק לוודא — האם תרצה להמשיך ולפתוח קריאה, או שאפשר לסגור את הפנייה?"
            send_message(phone, msg2)
            add_to_history(phone, "bot", f"[תזכורת 2] {msg2}")
            save_data()

        t2 = threading.Timer(300.0, remind_second)
        t2.daemon = True
        with state_lock:
            existing = reminder_timers.get(phone, [])
            if isinstance(existing, list):
                existing.append(t2)
            reminder_timers[phone] = existing if isinstance(existing, list) else [t2]
        t2.start()

    t1 = threading.Timer(300.0, remind_first)
    t1.daemon = True
    with state_lock:
        reminder_timers[phone] = [t1]
        reminder_timers[phone + "_cancel"] = cancelled
    t1.start()

def transcribe_audio_groq(audio_url):
    """תמלול הקלטה קולית עם Groq Whisper — חינמי, תומך עברית"""
    if not GROQ_API_KEY:
        print("[Groq] חסר GROQ_API_KEY", flush=True)
        return None
    try:
        r = requests.get(audio_url, timeout=15)
        if r.status_code != 200:
            print(f"[Groq] download failed: {r.status_code}", flush=True)
            return None
        # שלח ל-Groq Whisper
        resp = requests.post(
            GROQ_WHISPER_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.mp3", r.content, "audio/mpeg")},
            data={"model": "whisper-large-v3", "language": "he", "response_format": "text", "prompt": "שיחה בעברית על בריכות שחייה, כתובות בישראל, שמות ערים כמו: תל אביב, רמת גן, פתח תקווה, אבן יהודה, כפר סבא, נתניה, חולון, בת ים"},
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text.strip()
            print(f"[Groq] transcribed: {text[:60]}", flush=True)
            return text
        print(f"[Groq] error: {resp.status_code} {resp.text[:100]}", flush=True)
        return None
    except Exception as e:
        print(f"[Groq] exception: {e}", flush=True)
        return None

def do_open_wizenet(call_data, emails, client_phone):
    """פתיחת קריאה בפועל בויזנט"""
    wid = open_wizenet_call(call_data)
    if wid:
        call_data["wizenet_id"] = wid
        with state_lock:
            for c in service_calls:
                if c["id"] == call_data["id"]:
                    c["wizenet_id"] = wid
                    break
            _notify_phone = runtime_settings.get("notify_personal_phone", NOTIFY_PERSONAL_PHONE)
            _notify_group = runtime_settings.get("notify_group_id", NOTIFY_GROUP_ID)
            go_group = notify_to_group_state
        save_data()

        # הודעה ללקוח/בוס עם מספר קריאה
        msg_client = "✅ קריאה נפתחה בויזנט — מספר קריאה: #" + str(wid)
        send_message(client_phone, msg_client)
        add_to_history(client_phone, "bot", msg_client)

        # הודעה למורן/קבוצה עם כל הפרטים + מספר ויזנט
        name = call_data.get("name", "-")
        notify_msg = "\n".join([
            f"🔔 *קריאה חדשה — #{wid}*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 *לקוח:* {name}",
            f"📞 *טלפון:* {call_data.get('contact_phone', '-')}",
            f"📍 *כתובת:* {call_data.get('address', '-')}",
            f"🔧 *סוג:* {call_data.get('call_type', '-')}",
            f"📝 *תיאור:* {call_data.get('description', '-')}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🗂 *מספר קריאה בויזנט:* #{wid}",
            f"🕐 *נפתח:* {il_now().strftime('%d/%m/%Y %H:%M')}",
            "",
            "⚡ יש לזהות את הקריאה במערכת ולשבץ טכנאי."
        ])
        if go_group:
            send_message(_notify_group, notify_msg)
        else:
            send_message(_notify_phone, notify_msg)

        # מייל עם מספר ויזנט
        if emails:
            send_email_notification(call_data, emails)
    else:
        # ויזנט נכשל — שלח הודעה ידנית
        with state_lock:
            _notify_phone = runtime_settings.get("notify_personal_phone", NOTIFY_PERSONAL_PHONE)
            go_group = notify_to_group_state
            _notify_group = runtime_settings.get("notify_group_id", NOTIFY_GROUP_ID)
        fallback_msg = build_notify_message(client_phone, call_data)
        if go_group:
            send_message(_notify_group, fallback_msg)
        else:
            send_message(_notify_phone, fallback_msg)
        if emails:
            send_email_notification(call_data, emails)

    fire_webhook(call_data)

def handle_message(phone, body, msg_type="text", audio_url=None):
    is_boss = is_boss_phone(phone)
    print(f"[Handle] phone={phone} phone972={phone972(phone)} is_boss={is_boss}", flush=True)

    # תמלול הקלטה — לכולם (לקוחות ורועי)
    if msg_type == "audio" and audio_url:
        transcribed = transcribe_audio_groq(audio_url)
        if transcribed:
            body = transcribed
            msg_type = "text"
            # עדכן את ההיסטוריה הקיימת עם התמלול (הוספנו [שלח הקלטה קולית] קודם)
            with state_lock:
                hist = chat_history.get(phone, [])
                if hist and hist[-1].get("type") == "audio" and hist[-1].get("sender") == "client":
                    hist[-1]["message"] = f"🎤 {transcribed}"
        else:
            body = "[שלח הקלטה קולית — לא הצלחתי לתמלל]"

    with state_lock:
        cancel_reminder(phone)
        history = list(chat_history.get(phone, []))

    # בדוק אם ממתינים לאישור ויזנט מהלקוח
    with state_lock:
        pending = pending_wizenet_confirm.get(phone)

    if pending:
        answer = body.strip()
        answer_lower = answer.lower()
        is_yes = any(x in answer_lower for x in ["כן", "yes", "נכון", "אישור", "אשר", "ok"])
        is_no  = any(x in answer_lower for x in ["לא", "no", "שגוי", "לא נכון", "טעות"])

        # בחירה מרשימה (1-5)
        wiz_options = pending.get("wiz_options")
        if wiz_options and answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(wiz_options):
                chosen = wiz_options[idx]
                with state_lock:
                    pending_wizenet_confirm.pop(phone, None)
                call_data = pending["call_data"]
                call_data["cid_confirmed"] = chosen["cid"]
                threading.Thread(
                    target=do_open_wizenet,
                    args=(call_data, pending["emails"], pending["client_phone"]),
                    daemon=True
                ).start()
                reset_session(phone)
                add_to_history(phone, "bot", "[קריאה נפתחה — שיחה חדשה]", "text")
                return f"✅ מעולה, פותח קריאה על כרטיס *{chosen['name']}*"
            return f"בחר מספר בין 1 ל-{len(wiz_options)}, או 'לא' אם אף אחד לא מתאים"

        if is_yes:
            with state_lock:
                pending_wizenet_confirm.pop(phone, None)
            call_data = pending["call_data"]
            call_data["cid_confirmed"] = call_data.get("_wizenet_cid", "-1")
            reset_session(phone)
            add_to_history(phone, "bot", "[קריאה נפתחה — שיחה חדשה]", "text")
            threading.Thread(
                target=do_open_wizenet,
                args=(call_data, pending["emails"], pending["client_phone"]),
                daemon=True
            ).start()
            return "✅ מעולה, פותח את הקריאה על הכרטיס הנכון!"

        elif is_no:
            with state_lock:
                pending_wizenet_confirm.pop(phone, None)
            call_data = pending["call_data"]
            with state_lock:
                _notify_phone = runtime_settings.get("notify_personal_phone", NOTIFY_PERSONAL_PHONE)
            notify_manual = (
                "⚠️ *לקוח לא זוהה בויזנט*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "👤 *שם:* " + call_data.get("name","-") + "\n"
                "📞 *טלפון:* " + call_data.get("contact_phone","-") + "\n"
                "📝 *תיאור:* " + call_data.get("description","-") + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "נא לאתר את הכרטיס ולפתוח קריאה ידנית."
            )
            send_message(_notify_phone, notify_manual)
            return "מצטער, לא הצלחתי למצוא את הכרטיס שלך. נציג יצור איתך קשר בהקדם לטיפול 🙂"

        # לא הבין
        if wiz_options:
            return f"בחר מספר בין 1 ל-{len(wiz_options)}, או 'לא' אם אף אחד לא מתאים"
        wiz_name = pending.get("wiz_name", "")
        return "מצאתי לקוח: *" + wiz_name + "* — זה הכרטיס הנכון? ענה כן או לא"

    result = ask_claude(history, body, msg_type, is_boss=is_boss, phone=phone)
    action = result.get("action", "continue")

    if action == "open_call":
        # וולידציות — רק ללקוחות, לא לבוס
        contact_phone = result.get("contact_phone", "-")
        if not is_boss:
            addr_ok, addr_err = validate_address_basic(result.get("address", ""))
            if not addr_ok:
                return f"הכתובת לא נראית תקינה ({addr_err}). נסה שוב."
            if not validate_il_phone(contact_phone):
                return "מספר הטלפון לא תקין. נא לספק מספר ישראלי (05X)."

        with state_lock:
            cancel_reminder(phone)
            call_id = len(service_calls) + 1
            service_calls.append({
                "id": call_id,
                "phone": phone,
                "name": result.get("name", "-"),
                "address": result.get("address", "-"),
                "call_type": result.get("call_type", "-"),
                "description": result.get("description", "-"),
                "contact_phone": contact_phone,
                "opened_at": il_now().strftime("%d/%m/%Y %H:%M"),
                "status": "ממתינה לטיפול"
            })
            reset_session(phone)

        # ניתוב הודעה
        notify_msg = build_notify_message(phone, result)
        with state_lock:
            go_group = notify_to_group_state
            _notify_phone = runtime_settings.get("notify_personal_phone", NOTIFY_PERSONAL_PHONE)
            _notify_group = runtime_settings.get("notify_group_id", NOTIFY_GROUP_ID)

        if go_group:
            print(f"[Notify] → קבוצה {_notify_group}", flush=True)
            ok = send_message(_notify_group, notify_msg)
            print(f"[Notify] קבוצה ok={ok}", flush=True)
        else:
            print(f"[Notify] → אישי {_notify_phone}", flush=True)
            ok = send_message(_notify_phone, notify_msg)
            print(f"[Notify] אישי ok={ok}", flush=True)

        save_data()
        _call_copy = service_calls[-1].copy()

        # Wizenet + Webhook + מייל — הכל בthread נפרד, לא חוסם
        with state_lock:
            _emails = list(runtime_settings.get("notification_emails", []))

        _client_phone = phone

        def _background_tasks(call_data, emails, client_phone):
            # נרמל טלפון — קבל גם +972, 972, 05X
            contact_phone = normalize_il_phone(call_data.get("contact_phone", "").strip())
            call_data["contact_phone"] = contact_phone
            client_name = call_data.get("name", "").strip()

            # שלב 1 — חפש לפי טלפון
            client_info = None
            if validate_il_phone(contact_phone):
                client_info = get_wizenet_client_by_phone(contact_phone)
                if client_info:
                    print(f"[Wizenet] נמצא לפי טלפון: {client_info['name']}", flush=True)

            # שלב 2 — אם לא נמצא לפי טלפון, חפש לפי שם + עיר
            city = ""
            address = call_data.get("address", "")
            # חלץ עיר מהכתובת — המילה האחרונה לרוב
            if address:
                parts = address.replace(",", " ").split()
                if parts:
                    city = parts[-1]
            if not client_info and client_name:
                results = get_wizenet_client_by_name(client_name, city=city)
                if len(results) == 1:
                    client_info = results[0]
                    print(f"[Wizenet] נמצא לפי שם: {client_info['name']}", flush=True)
                elif len(results) > 1:
                    options = "\n".join([str(i+1) + ". " + r["name"] for i, r in enumerate(results[:5])])
                    confirm_msg = "מצאתי כמה לקוחות:\n" + options + "\n\nאיזה מספר נכון? או 'לא' אם אף אחד"
                    with state_lock:
                        pending_wizenet_confirm[client_phone] = {
                            "call_data": call_data, "emails": emails,
                            "client_phone": client_phone, "wiz_options": results[:5]
                        }
                    send_message(client_phone, confirm_msg)
                    add_to_history(client_phone, "bot", confirm_msg)
                    return

            if client_info:
                wiz_name = client_info["name"]
                call_data["_wizenet_cid"] = client_info["cid"]
                with state_lock:
                    pending_wizenet_confirm[client_phone] = {
                        "call_data": call_data, "emails": emails,
                        "client_phone": client_phone, "wiz_name": wiz_name
                    }
                confirm_msg = "מצאתי לקוח: *" + wiz_name + "*\nזה הכרטיס הנכון? (כן / לא)"
                send_message(client_phone, confirm_msg)
                add_to_history(client_phone, "bot", confirm_msg)
            else:
                # לא נמצא לקוח — לא פותחים קריאה, מודיעים לרועי לטיפול ידני
                with state_lock:
                    _notify_phone = runtime_settings.get("notify_personal_phone", NOTIFY_PERSONAL_PHONE)
                notify_msg = (
                    "⚠️ *לקוח לא זוהה — נדרש טיפול ידני*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "👤 *שם:* " + call_data.get("name", "-") + "\n"
                    "📞 *טלפון:* " + call_data.get("contact_phone", "-") + "\n"
                    "📍 *כתובת:* " + call_data.get("address", "-") + "\n"
                    "📝 *תיאור:* " + call_data.get("description", "-") + "\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "נא לאתר את הכרטיס ולפתוח קריאה ידנית בויזנט."
                )
                send_message(_notify_phone, notify_msg)
                print(f"[Wizenet] לא נמצא לקוח — נשלחה התראה לרועי", flush=True)
                # הודע ללקוח
                if not is_boss_phone(client_phone):
                    send_message(client_phone, "קיבלנו את הפנייה שלך! נציג יצור איתך קשר בהקדם 🙂")
                    add_to_history(client_phone, "bot", "קיבלנו את הפנייה שלך! נציג יצור איתך קשר בהקדם 🙂")

        threading.Thread(target=_background_tasks, args=(_call_copy, _emails, _client_phone), daemon=True).start()

        if is_boss:
            reset_session(phone)
            # הוסף הודעת מערכת שמונעת פתיחה כפולה
            add_to_history(phone, "bot", "[קריאה נפתחה — שיחה חדשה]", "text")
            return "✅ הקריאה נפתחה"
        return "✅ הפנייה התקבלה בהצלחה!\nנציג יצור איתך קשר בהקדם 🙂\nלכל שאלה נוספת — אנחנו כאן."

    if action == "send_message" and is_boss:
        target = result.get("phone", "")
        msg_to_send = result.get("message", "")
        if target and msg_to_send:
            target = phone972(target)
            sent = send_message(target, msg_to_send)
            reset_session(phone)
            return f"נשלח ל-{target}" if sent else "שגיאה בשליחה"
        return "חסרים פרטים"

    if action == "cancelled":
        with state_lock:
            cancel_reminder(phone)
            reset_session(phone)
        save_data()
        # אם קלוד שלח הודעה מותאמת (למשל זיהוי צחוק) — השתמש בה
        return result.get("message") or "בסדר. אם תצטרך עזרה — אנחנו כאן."

    reply = result.get("message", "לא הבנתי, נסה שוב.")
    if not is_boss:
        schedule_reminder(phone, reply)
    return reply

def process_green_event(body, receipt_id=None):
    if is_duplicate_green_event(body, receipt_id):
        return
    webhook_type = body.get("typeWebhook", "")
    msg_data = body.get("messageData", {})
    sender = body.get("senderData", {})

    def get_phone():
        return sender.get("chatId", "").replace("@c.us", "")

    if webhook_type == "incomingMessageReceived":
        phone = get_phone()
        if not phone or is_group(phone + "@c.us"):
            return
        msg_type, body_text = parse_green_msg(msg_data)
        # שיחת וואטסאפ נכנסת — הוסף לרשימה גם ללא טקסט
        # מילוי טקסט ברירת מחדל לסוגי הודעות ללא טקסט
        if not body_text:
            type_msg = msg_data.get("typeMessage", "")
            if type_msg == "callMessage":
                body_text = "[התקשר בשיחת וואטסאפ]"
            elif type_msg == "audioMessage":
                body_text = "[שלח הקלטה קולית]"
                msg_type = "audio"
            elif type_msg == "imageMessage":
                body_text = "[שלח תמונה]"
                msg_type = "image"
            elif type_msg == "videoMessage":
                body_text = "[שלח וידאו]"
                msg_type = "video"
            elif type_msg == "documentMessage":
                body_text = "[שלח מסמך]"
                msg_type = "document"
            elif type_msg == "stickerMessage":
                body_text = "[שלח סטיקר]"
                msg_type = "sticker"
            else:
                return  # סוג לא מוכר — מדלג

        is_boss = is_boss_phone(phone)
        audio_url = extract_audio_url(msg_data) if msg_type == "audio" else None
        if msg_type == "audio":
            print(f"[Audio] msg_data keys: {list(msg_data.keys())}", flush=True)
            print(f"[Audio] audio_url: {audio_url}", flush=True)

        with state_lock:
            # AUTO_BOT=False — לעולם לא מפעיל אוטומטית, אלא אם כן הבוס
            if phone not in bot_enabled:
                bot_enabled[phone] = is_boss  # הבוס תמיד פעיל, שאר - כבוי
            sessions.setdefault(phone, {"step": "active", "data": {}})

        # תמיד נוסיף להיסטוריה — גם הקלטות
        add_to_history(phone, "client", body_text, msg_type)
        save_data()

        with state_lock:
            allow_reply = bot_enabled.get(phone, False) and global_bot_on

        # סטיקר — שומר להיסטוריה אבל לא עונה
        if msg_type == "sticker":
            return

        # הודעה קצרה/לא ברורה (נקודה, אות, ריק) — בכל זאת ענה
        if body_text and len(body_text.strip()) <= 2 and not is_boss:
            body_text = body_text.strip()

        # הודעה ריקה / נקודה / תו בודד — תגיב בכל מקרה
        if body_text.strip() in [".", ",", "-", "?", "!", " ", ""] or len(body_text.strip()) <= 1:
            body_text = body_text.strip() or "."

        if allow_reply:
            # מניעת עיבוד מקביל — שומר הודעה ממתינה במקום לזרוק
            with state_lock:
                started = processing_phones.get(phone)
                if started and (time.time() - started) < PROCESSING_TIMEOUT:
                    pending_messages[phone] = (body_text, msg_type, audio_url)
                    print(f"[Queue] {phone} בעיבוד — הודעה נשמרה לתור", flush=True)
                    return
                processing_phones[phone] = time.time()
            try:
                current_body, current_type, current_audio = body_text, msg_type, audio_url
                while True:
                    reply = handle_message(phone, current_body, current_type, audio_url=current_audio)
                    add_to_history(phone, "bot", reply)
                    send_message(phone, reply)
                    save_data()
                    # בדוק אם הגיעה הודעה נוספת בזמן העיבוד
                    with state_lock:
                        nxt = pending_messages.pop(phone, None)
                        if nxt:
                            processing_phones[phone] = time.time()
                    if not nxt:
                        break
                    current_body, current_type, current_audio = nxt
            finally:
                with state_lock:
                    processing_phones.pop(phone, None)
                    pending_messages.pop(phone, None)

    elif webhook_type == "incomingCall":
        phone = sender.get("chatId", "").replace("@c.us", "")
        if not phone or is_group(phone + "@c.us"):
            return
        with state_lock:
            chat_history.setdefault(phone, [])
            bot_enabled.setdefault(phone, is_boss_phone(phone))
            sessions.setdefault(phone, {"step": "active", "data": {}})
        add_to_history(phone, "client", "[התקשר בשיחת וואטסאפ]", "call")
        save_data()
        # ענה אם הבוט פעיל
        with state_lock:
            allow_reply = bot_enabled.get(phone, False) and global_bot_on
        if allow_reply:
            reply = handle_message(phone, "[התקשר בשיחת וואטסאפ]", "text", phone=phone)
            add_to_history(phone, "bot", reply)
            send_message(phone, reply)
            save_data()

    elif webhook_type == "outgoingMessageReceived":
        phone = get_phone()
        if not phone or is_group(phone + "@c.us"):
            return
        _, body_text = parse_green_msg(msg_data)
        if not body_text:
            return
        with state_lock:
            # רק מוסיפים להיסטוריה — לא משנים מצב הבוט בכלל
            sessions.setdefault(phone, {"step": "active", "data": {}})
        add_to_history(phone, "bot", body_text, "text")
        save_data()

# ─── Webhook ──────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Webhook-Secret", "")
            sec = WEBHOOK_SECRET.encode("utf-8")
            tok = token.encode("utf-8")
            if len(tok) != len(sec) or not hmac.compare_digest(tok, sec):
                return "forbidden", 403
        data = request.get_json(force=True)
        if not data:
            return "ok"
        print(f"[Webhook] type={data.get('typeWebhook','')} sender={data.get('senderData',{})}", flush=True)
        process_green_event(data, None)
    except Exception as e:
        print(f"[Webhook] error: {e}", flush=True)
    return "ok"

# ─── Login ────────────────────────────────────────────────────
LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>כניסה</title>
<style>body{font-family:system-ui,sans-serif;max-width:380px;margin:48px auto;padding:24px;background:#0b0d12;color:#dde1ec;}
label{display:block;font-size:14px;margin-bottom:6px;color:#5a6378}
input{width:100%;box-sizing:border-box;padding:12px;margin-bottom:14px;border-radius:10px;border:1px solid #252b3b;background:#1a1e2a;color:#dde1ec;font-size:16px}
button{width:100%;padding:14px;background:#25d366;color:#000;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer}
.err{color:#e74c3c;margin-top:10px;font-size:14px}
h2{margin-bottom:18px;font-size:20px}</style></head>
<body>
<h2>כניסה לפאנל</h2>
<form method="post">
<label for="pw">סיסמה</label>
<input id="pw" type="password" name="password" required autocomplete="current-password">
{% if err %}<div class="err">{{ err }}</div>{% endif %}
<button type="submit">כניסה</button>
</form>
</body></html>"""

@app.route("/health")
def health_check():
    return jsonify({"ok": True})

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not AUTH_CONFIGURED:
        return redirect(url_for("dashboard"))
    if session.get("admin"):
        return redirect(request.args.get("next") or url_for("dashboard"))
    err = None
    if request.method == "POST" and (ADMIN_PASSWORD or ADMIN_TOKEN):
        pw = request.form.get("password", "")
        ok_pw = ADMIN_PASSWORD and hmac.compare_digest(
            hashlib.sha256(pw.encode()).digest(),
            hashlib.sha256(ADMIN_PASSWORD.encode()).digest()
        )
        ok_tok = ADMIN_TOKEN and hmac.compare_digest(
            hashlib.sha256(pw.encode()).digest(),
            hashlib.sha256(ADMIN_TOKEN.encode()).digest()
        )
        if ok_pw or ok_tok:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        err = "סיסמה שגויה"
    return render_template_string(LOGIN_PAGE_HTML, err=err)

def _last_msg_ts_key(last_msg):
    if not last_msg:
        return 0.0
    s = (last_msg.get("ts") or "").replace("Z", "+00:00")
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0

# ─── API ──────────────────────────────────────────────────────
@app.route("/api/chats")
def api_chats():
    search = request.args.get("q", "").strip().lower()
    with state_lock:
        all_phones = set(list(chat_history.keys()) + list(bot_enabled.keys()))
        snapshot = []
        for phone in all_phones:
            if is_group(phone + "@c.us"):
                continue
            history = list(chat_history.get(phone, []))
            last = history[-1] if history else None
            if search:
                phone_match = search in phone.replace("972", "0", 1)
                text_match = any(search in h["message"].lower() for h in history)
                if not phone_match and not text_match:
                    continue
            snapshot.append({
                "phone": phone,
                "bot_active": bot_enabled.get(phone, False),
                "greeting_sent": greeting_sent.get(phone, False),
                "last_message": last,
                "msg_count": len(history),
                "step": sessions.get(phone, {}).get("step", "active"),
            })
    with state_lock:
        g_on = global_bot_on
    for c in snapshot:
        c["_sort"] = (
            0 if (c["bot_active"] and g_on) else 1,
            -_last_msg_ts_key(c["last_message"]),
        )
    snapshot.sort(key=lambda c: c["_sort"])
    for c in snapshot:
        del c["_sort"]
    return jsonify(snapshot)

@app.route("/api/chat-history/<path:phone>")
def api_chat_history(phone):
    with state_lock:
        history = list(chat_history.get(phone, []))
    return jsonify(history)

@app.route("/api/status")
def api_status():
    """endpoint יחיד שמחזיר הכל — מפחית בקשות מהפורטל"""
    search = request.args.get("q", "").strip().lower()
    with state_lock:
        all_phones = set(list(chat_history.keys()) + list(bot_enabled.keys()))
        snapshot = []
        for phone in all_phones:
            if is_group(phone + "@c.us"):
                continue
            history = list(chat_history.get(phone, []))
            last = history[-1] if history else None
            if search:
                phone_match = search in phone.replace("972", "0", 1)
                text_match = any(search in h["message"].lower() for h in history)
                if not phone_match and not text_match:
                    continue
            snapshot.append({
                "phone": phone,
                "bot_active": bot_enabled.get(phone, False),
                "greeting_sent": greeting_sent.get(phone, False),
                "last_message": last,
                "msg_count": len(history),
                "step": sessions.get(phone, {}).get("step", "active"),
            })
        g_on = global_bot_on
        ng = notify_to_group_state
        calls_snap = list(service_calls)

    for c in snapshot:
        c["_sort"] = (
            0 if (c["bot_active"] and g_on) else 1,
            -_last_msg_ts_key(c["last_message"]),
        )
    snapshot.sort(key=lambda c: c["_sort"])
    for c in snapshot:
        del c["_sort"]

    return jsonify({
        "chats": snapshot,
        "calls": calls_snap,
        "global_bot_on": g_on,
        "notify_to_group": ng
    })

@app.route("/api/global-toggle", methods=["POST"])
def api_global_toggle():
    global global_bot_on
    with state_lock:
        global_bot_on = not global_bot_on
        v = global_bot_on
    save_data()
    return jsonify({"global_bot_on": v})

@app.route("/api/global-status")
def api_global_status():
    with state_lock:
        v = global_bot_on
        ng = notify_to_group_state
    return jsonify({"global_bot_on": v, "notify_to_group": ng})

@app.route("/api/notify-toggle", methods=["POST"])
def api_notify_toggle():
    """מתג ניתוב קריאות: קבוצה ↔ מורן אישי"""
    global notify_to_group_state
    with state_lock:
        notify_to_group_state = not notify_to_group_state
        v = notify_to_group_state
    save_data()
    target = f"קבוצה ({NOTIFY_GROUP_ID})" if v else f"מורן אישי ({NOTIFY_PERSONAL_PHONE})"
    print(f"[Notify] ניתוב שונה ל: {target}", flush=True)
    return jsonify({"notify_to_group": v, "target": target})

@app.route("/api/sync-chats", methods=["POST"])
def api_sync_chats():
    if not GREEN_API_URL or not GREEN_API_TOKEN:
        return jsonify({"ok": False, "error": "Green API לא מוגדר"})
    try:
        url = f"{GREEN_API_URL}/getChats/{GREEN_API_TOKEN}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Green API: {r.status_code}"})
        chats_data = r.json()
        if not isinstance(chats_data, list):
            return jsonify({"ok": False, "error": "תגובה לא צפויה"})
        count = 0
        with state_lock:
            for chat in chats_data:
                chat_id = chat.get("id", "")
                if "@c.us" not in chat_id or "@g.us" in chat_id:
                    continue
                ph = chat_id.replace("@c.us", "")
                chat_history.setdefault(ph, [])
                bot_enabled.setdefault(ph, False)
                sessions.setdefault(ph, {"step": "active", "data": {}})
                count += 1
        save_data()
        return jsonify({"ok": True, "synced": count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/enable-all", methods=["POST"])
def api_enable_all():
    with state_lock:
        n = 0
        for ph in list(chat_history.keys()):
            if not bot_enabled.get(ph, False):
                bot_enabled[ph] = True
                n += 1
    save_data()
    return jsonify({"ok": True, "enabled": n})

@app.route("/api/disable-all", methods=["POST"])
def api_disable_all():
    with state_lock:
        phones = list(bot_enabled.keys())
        for ph in phones:
            bot_enabled[ph] = False
    for ph in phones:
        cancel_reminder(ph)
    save_data()
    return jsonify({"ok": True})

@app.route("/api/toggle/<path:phone>", methods=["POST"])
def api_toggle(phone):
    with state_lock:
        bot_enabled[phone] = not bot_enabled.get(phone, False)
        now_active = bot_enabled[phone]
        need_greeting = now_active and not greeting_sent.get(phone, False)

    if not now_active:
        cancel_reminder(phone)
        save_data(sync_firestore=True)
        return jsonify({"phone": phone, "bot_active": False})

    # הפעלה — שמור מיד ושלח ברכה ב-thread נפרד
    with state_lock:
        sessions.setdefault(phone, {"step": "active", "data": {}})
    save_data(sync_firestore=True)

    if need_greeting:
        def _send_greet():
            greet = f"{get_greeting()}! איך אפשר לעזור?"
            sent = send_message(phone, greet)
            if sent:
                with state_lock:
                    greeting_sent[phone] = True
                add_to_history(phone, "bot", greet)
                save_data()
            print(f"[Toggle] ברכה {'נשלחה' if sent else 'נכשלה'} ל-{phone}", flush=True)
        threading.Thread(target=_send_greet, daemon=True).start()

    return jsonify({"phone": phone, "bot_active": True})

@app.route("/api/add-contact", methods=["POST"])
def api_add_contact():
    data = request.get_json(force=True)
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "נדרש מספר טלפון"})
    phone = phone972(phone)
    with state_lock:
        chat_history.setdefault(phone, [])
        bot_enabled.setdefault(phone, False)
        sessions.setdefault(phone, {"step": "active", "data": {}})
    save_data()
    return jsonify({"ok": True, "phone": phone})

@app.route("/api/send-greeting/<path:phone>", methods=["POST"])
def api_send_greeting(phone):
    """שלח הודעת פתיחה מחדש — גם אם כבר נשלחה בעבר"""
    greet = f"{get_greeting()}! איך אפשר לעזור?"
    sent = send_message(phone, greet)
    if sent:
        with state_lock:
            greeting_sent[phone] = True
            bot_enabled[phone] = True
            sessions[phone] = {"step": "active", "data": {}}
        add_to_history(phone, "bot", greet)
        save_data()
    return jsonify({"ok": sent})

@app.route("/api/resend-last/<path:phone>", methods=["POST"])
def api_resend_last(phone):
    with state_lock:
        history = list(chat_history.get(phone, []))
    bot_msgs = [h for h in history if h["sender"] == "bot"]
    if not bot_msgs:
        return jsonify({"ok": False, "error": "אין הודעות בוט"})
    last_msg = bot_msgs[-1]["message"]
    if last_msg.startswith("[תזכורת] "):
        last_msg = last_msg[9:]
    if last_msg.startswith("[נשלח שוב] "):
        last_msg = last_msg[11:]
    sent = send_message(phone, last_msg)
    if sent:
        add_to_history(phone, "bot", f"[נשלח שוב] {last_msg}")
        save_data()
    return jsonify({"ok": sent})

@app.route("/api/service-calls")
def api_service_calls():
    with state_lock:
        return jsonify(list(service_calls))

@app.route("/api/service-calls/<int:call_id>/status", methods=["POST"])
def api_update_status(call_id):
    data = request.get_json(force=True)
    with state_lock:
        for call in service_calls:
            if call["id"] == call_id:
                call["status"] = data.get("status", "")
                save_data()
                return jsonify({"ok": True})
    return jsonify({"ok": False}), 404



@app.route("/api/service-calls/<int:call_id>", methods=["DELETE"])
def api_delete_call(call_id):
    with state_lock:
        global service_calls
        service_calls = [c for c in service_calls if c["id"] != call_id]
    save_data(sync_firestore=True)
    return jsonify({"ok": True})

@app.route("/api/service-calls/clear", methods=["POST"])
def api_clear_calls():
    with state_lock:
        global service_calls
        service_calls = []
    save_data(sync_firestore=True)
    return jsonify({"ok": True})

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    with state_lock:
        return jsonify(dict(runtime_settings))

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    global runtime_settings
    data = request.get_json(force=True)
    allowed = ["notify_personal_phone","notify_group_id","boss_phone","webhook_url","webhook_headers","notification_emails"]
    with state_lock:
        for k in allowed:
            if k in data:
                runtime_settings[k] = data[k]
    save_data()
    return jsonify({"ok": True})

def send_email_notification(call_data, emails):
    """שלח מייל התראה בפתיחת קריאה דרך Resend"""
    if not RESEND_API_KEY or not emails:
        return
    try:
        wid = call_data.get("wizenet_id", "")
        subject = f"קריאה חדשה #{wid} — {call_data.get('name','-')} | {call_data.get('call_type','-')}"
        wizenet_row = f'<tr><td style="padding:8px;color:#666">🗂 ויזנט</td><td style="padding:8px"><b>#{wid}</b></td></tr>' if wid else ""
        body = f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto">
  <h2 style="color:#25d366">🔔 קריאה חדשה נפתחה — #{wid}</h2>
  <p style="color:#e74c3c;font-weight:bold">יש לזהות את הקריאה במערכת ולשבץ טכנאי</p>
  <table style="width:100%;border-collapse:collapse">
    {wizenet_row}
    <tr><td style="padding:8px;color:#666">👤 שם</td><td style="padding:8px"><b>{call_data.get('name','-')}</b></td></tr>
    <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">📞 טלפון</td><td style="padding:8px">{call_data.get('contact_phone','-')}</td></tr>
    <tr><td style="padding:8px;color:#666">📍 כתובת</td><td style="padding:8px">{call_data.get('address','-')}</td></tr>
    <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">🔧 סוג</td><td style="padding:8px">{call_data.get('call_type','-')}</td></tr>
    <tr><td style="padding:8px;color:#666">📝 תיאור</td><td style="padding:8px">{call_data.get('description','-')}</td></tr>
    <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">🕐 נפתח</td><td style="padding:8px">{call_data.get('opened_at','-')}</td></tr>
  </table>
</div>"""
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": emails, "subject": subject, "html": body},
            timeout=10
        )
        print(f"[Resend] status={r.status_code}", flush=True)
    except Exception as e:
        print(f"[Resend] error: {e}", flush=True)

def _wizenet_headers():
    """Bearer TOKEN כפי שמוריה הנחתה"""
    token = WIZENET_API_TOKEN.strip()
    if token.lower().startswith("bearer "):
        auth = token
    else:
        auth = f"Bearer {token}"
    print(f"[Wizenet] Auth: {auth[:40]}", flush=True)
    return {"Authorization": auth, "Content-Type": "application/json"}

def _wizenet_search(ccell="", ccompany="", ccity=""):
    """חיפוש לקוח ב-Wizenet — מחזיר רשימת תוצאות"""
    if not WIZENET_API_TOKEN:
        return []
    payload = {}
    if ccell:
        payload["ccell"] = ccell
    if ccompany:
        payload["ccompany"] = ccompany
    if ccity:
        payload["ccity"] = ccity
    if not payload:
        return []
    try:
        r = requests.post(
            f"{WIZENET_BASE_URL}/?func=wizeApp_retClientExist",
            headers=_wizenet_headers(),
            json=payload,
            timeout=10
        )
        print(f"[Wizenet/Search] payload={payload} status={r.status_code} response={r.text[:300]}", flush=True)
        if r.status_code == 200 and r.text.strip().startswith("["):
            data = r.json()
            results = []
            for item in data:
                cid = str(item.get("cid") or item.get("CID") or "-1")
                name = (item.get("Ccompany") or item.get("ccompany") or item.get("name") or "").strip()
                city = (item.get("Ccity") or item.get("ccity") or "").strip()
                if cid != "-1" and name:
                    results.append({"cid": cid, "name": name, "city": city})
            print(f"[Wizenet/Search] נמצאו {len(results)} תוצאות", flush=True)
            return results
    except Exception as e:
        print(f"[Wizenet/Search] exception: {e}", flush=True)
    return []

def get_wizenet_client_by_phone(contact_phone):
    """חיפוש לפי טלפון"""
    if not contact_phone:
        return None
    phone_local = normalize_il_phone(contact_phone)
    results = _wizenet_search(ccell=phone_local)
    return results[0] if results else None

def get_wizenet_client_by_name(client_name, city=""):
    """חיפוש לפי שם — מנסה גם שם הפוך"""
    if not client_name or len(client_name.strip()) < 2:
        return []
    results = []
    # נסה שם כפי שהוא
    r1 = _wizenet_search(ccompany=client_name.strip(), ccity=city)
    results.extend(r1)
    # נסה שם הפוך (ניצה חן → חן ניצה)
    parts = client_name.strip().split()
    if len(parts) == 2:
        reversed_name = parts[1] + " " + parts[0]
        r2 = _wizenet_search(ccompany=reversed_name, ccity=city)
        # הוסף רק תוצאות חדשות
        existing_cids = {r["cid"] for r in results}
        for r in r2:
            if r["cid"] not in existing_cids:
                results.append(r)
    return results

def get_wizenet_client(contact_phone):
    """לתאימות לאחור"""
    return get_wizenet_client_by_phone(contact_phone)

def get_wizenet_cid(contact_phone):
    """לתאימות לאחור"""
    client = get_wizenet_client_by_phone(contact_phone)
    return client["cid"] if client else "-1"

def _call_type_to_id(call_type):
    """המרת סוג קריאה למספר בויזנט"""
    ct = str(call_type).lower()
    if any(x in ct for x in ["פרויקט", "בנייה", "אבזור", "בינוי", "גמר"]):
        return "18"
    if any(x in ct for x in ["תחזוקה", "מים", "ניטור", "תקלה"]):
        return "21"
    if any(x in ct for x in ["חשמל"]):
        return "23"
    if any(x in ct for x in ["שיפוץ", "חידוש"]):
        return "20"
    return "21"  # ברירת מחדל — תחזוקה

def open_wizenet_call(call_data):
    """פתיחת קריאה ב-Wizenet"""
    if not WIZENET_API_TOKEN or not WIZENET_URL:
        return None
    try:
        contact_phone = call_data.get("contact_phone", "")
        # אם CID כבר אושר — השתמש בו, אחרת חפש
        cid = call_data.get("cid_confirmed") or call_data.get("_wizenet_cid") or get_wizenet_cid(contact_phone)
        call_type_id = _call_type_to_id(call_data.get("call_type", ""))
        tech_name = call_data.get("tech_name", "").strip()
        print(f"[Wizenet] פותח קריאה CID={cid} טלפון={contact_phone} calltype={call_type_id} טכנאי={tech_name}", flush=True)

        payload = {
            "callid": "-1",
            "cid": cid,
            "subject": call_data.get('description', call_data.get('call_type', 'שירות'))[:80],
            "ccell": contact_phone,
            "CntctName": call_data.get("name", ""),
            "comments": call_data.get('description', '') if call_data.get('cid_confirmed', '-1') != '-1' else f"{call_data.get('description', '')} | כתובת: {call_data.get('address', '')}",
            "OriginID": "5",
            "calltypeid": call_type_id,
            "statusid": "113",
        }
        if tech_name:
            payload["TechName"] = tech_name
        r = requests.post(
            WIZENET_URL,
            headers=_wizenet_headers(),
            json=payload,
            timeout=10
        )
        print(f"[Wizenet] status={r.status_code} response={r.text[:300]}", flush=True)
        if r.status_code == 200 and r.text.strip().startswith("["):
            data = r.json()
            if isinstance(data, list) and data:
                status = data[0].get("Status")
                call_id = data[0].get("CALLID")
                if status == "1":
                    print(f"[Wizenet] ✅ קריאה נפתחה: #{call_id}", flush=True)
                    return call_id
                else:
                    print(f"[Wizenet] ❌ שגיאה: {data[0].get('message')}", flush=True)
        else:
            print(f"[Wizenet] HTTP {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[Wizenet] exception: {e}", flush=True)
    return None

def fire_webhook(call_data):
    """שלח webhook חיצוני בפתיחת קריאה"""
    with state_lock:
        url = runtime_settings.get("webhook_url","").strip()
        headers_raw = runtime_settings.get("webhook_headers","").strip()
    if not url:
        return
    try:
        headers = {"Content-Type": "application/json"}
        if headers_raw:
            for line in headers_raw.split("\n"):
                if ":" in line:
                    k,v = line.split(":",1)
                    headers[k.strip()] = v.strip()
        requests.post(url, json=call_data, headers=headers, timeout=8)
        print(f"[Webhook] fired to {url}", flush=True)
    except Exception as e:
        print(f"[Webhook] error: {e}", flush=True)

# ─── Dashboard ────────────────────────────────────────────────
DASHBOARD = r"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>מרכז שירות לקוחות</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;500;600;700;900&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:{{PORTAL_BG}};--s1:#13161f;--s2:#1a1e2a;--s3:#222736;--border:#252b3b;--accent:{{PORTAL_ACCENT}};--text:#dde1ec;--muted:#5a6378;--danger:#e74c3c;}
body{font-family:'Heebo',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{background:var(--s1);border-bottom:1px solid var(--border);padding:0 20px;height:56px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;gap:12px}
.logo{display:flex;align-items:center;gap:9px;font-weight:800;font-size:16px}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,var(--accent),#128c7e);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:16px}
.hdr-mid{display:flex;align-items:center;gap:8px;flex:1;max-width:800px;flex-wrap:wrap}
.search-box{flex:1;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none}
.search-box:focus{border-color:var(--accent)}
.search-box::placeholder{color:var(--muted)}
.btn-hdr{border:none;border-radius:8px;padding:6px 13px;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.btn-notify.group{background:rgba(52,152,219,.2);color:#3498db;border:1px solid #3498db}
.btn-notify.personal{background:rgba(155,89,182,.2);color:#9b59b6;border:1px solid #9b59b6}
.btn-green{background:rgba(37,211,102,.2);color:var(--accent);border:1px solid var(--accent)}
.btn-red{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.stats{display:flex;gap:5px}
.stat{background:var(--s2);border:1px solid var(--border);border-radius:7px;padding:4px 10px;font-size:11px;color:var(--muted)}
.stat b{color:var(--text);font-size:13px}
.main{display:flex;flex:1;overflow:hidden}
.sidebar{width:300px;border-left:1px solid var(--border);background:var(--s1);display:flex;flex-direction:column;flex-shrink:0}
.sb-head{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.sb-title{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
.btn-add-contact{background:var(--accent);color:#000;border:none;border-radius:7px;padding:5px 10px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer}
.chat-list{flex:1;overflow-y:auto}
.chat-list::-webkit-scrollbar{width:3px}
.chat-list::-webkit-scrollbar-thumb{background:var(--border)}
.ci{padding:10px 12px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;align-items:center;gap:9px;transition:background .12s}
.ci:hover{background:var(--s2)}
.ci.active{background:var(--s2);border-right:3px solid var(--accent)}
.av{width:36px;height:36px;border-radius:50%;background:var(--s3);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0;position:relative}
.dot{position:absolute;bottom:-1px;left:-1px;width:11px;height:11px;border-radius:50%;border:2px solid var(--s1);background:var(--muted)}
.dot.on{background:var(--accent);box-shadow:0 0 5px var(--accent)}
.ci-info{flex:1;min-width:0}
.ci-phone{font-size:12px;font-weight:600;margin-bottom:1px}
.ci-last{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tgl{position:relative;width:34px;height:19px;display:inline-block;flex-shrink:0}
.tgl input{opacity:0;width:0;height:0}
.tsl{position:absolute;cursor:pointer;inset:0;background:var(--border);border-radius:19px;transition:.25s}
.tsl:before{content:"";position:absolute;height:13px;width:13px;right:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
input:checked+.tsl{background:var(--accent)}
input:checked+.tsl:before{transform:translateX(-15px)}
.chat-win{flex:1;display:flex;flex-direction:column;background:var(--bg);min-width:0}
.topbar{padding:10px 18px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;gap:8px;position:relative;z-index:1}
.tb-left{display:flex;align-items:center;gap:9px}
.tb-phone{font-weight:700;font-size:14px}
.tb-sub{font-size:11px;color:var(--muted)}
.tb-right{display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.badge{padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700;background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.badge.on{background:rgba(37,211,102,.12);color:var(--accent);border-color:rgba(37,211,102,.4)}
.btn-sm{border:none;border-radius:7px;padding:5px 11px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-act{background:var(--accent);color:#000}
.btn-deact{background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.btn-resend{background:var(--s3);color:var(--text);border:1px solid var(--border)}
.messages{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:7px;min-height:0}
.messages::-webkit-scrollbar{width:3px}
.messages::-webkit-scrollbar-thumb{background:var(--border)}
.msg{max-width:65%;padding:8px 12px;border-radius:11px;font-size:13px;line-height:1.5;white-space:pre-wrap;animation:fi .15s ease}
@keyframes fi{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.18);align-self:flex-start;border-bottom-left-radius:3px}
.msg-meta{font-size:9px;color:var(--muted);margin-top:2px}
.msg.client .msg-meta{text-align:right}
.calls-panel{width:260px;border-right:1px solid var(--border);background:var(--s1);display:flex;flex-direction:column;flex-shrink:0}
.cp-head{padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
.calls-list{flex:1;overflow-y:auto;padding:8px}
.call-card{background:var(--s2);border:1px solid var(--border);border-radius:9px;padding:10px;margin-bottom:7px;font-size:11px}
.call-id{font-size:9px;color:var(--muted);margin-bottom:3px}
.call-name{font-weight:700;font-size:12px;margin-bottom:2px}
.call-type{color:var(--accent);font-size:10px;margin-bottom:5px}
.call-row{color:var(--muted);margin-bottom:2px}
.call-row span{color:var(--text)}
.status-sel{margin-top:6px;width:100%;background:var(--s3);border:1px solid var(--border);border-radius:5px;padding:4px 7px;color:var(--text);font-family:inherit;font-size:11px;outline:none;cursor:pointer}
.empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);gap:8px}
.empty-icon{font-size:38px;opacity:.3}
.no-items{padding:24px 10px;text-align:center;color:var(--muted);font-size:11px;line-height:1.6}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:24px;width:320px}
.modal h3{margin-bottom:14px;font-size:16px}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-family:inherit;font-size:14px;outline:none;margin-bottom:12px;direction:ltr}
.modal input:focus{border-color:var(--accent)}
.modal-btns{display:flex;gap:8px;justify-content:flex-end}
.btn-cancel{background:var(--s2);color:var(--muted);border:1px solid var(--border);border-radius:7px;padding:7px 14px;font-family:inherit;font-size:13px;cursor:pointer}
.btn-confirm{background:var(--accent);color:#000;border:none;border-radius:7px;padding:7px 14px;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer}
.notify-info{font-size:10px;color:var(--muted);padding:4px 14px;border-bottom:1px solid var(--border)}
.btn-danger{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.settings-modal{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:24px;width:460px;max-height:80vh;overflow-y:auto}
.settings-modal h3{margin-bottom:16px;font-size:16px}
.setting-group{margin-bottom:16px}
.setting-label{font-size:12px;color:var(--muted);margin-bottom:5px;display:block}
.setting-input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none;direction:ltr}
.setting-input:focus{border-color:var(--accent)}
.setting-textarea{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:12px;outline:none;direction:ltr;resize:vertical;min-height:60px}
.setting-textarea:focus{border-color:var(--accent)}
.setting-hint{font-size:10px;color:var(--muted);margin-top:3px}
@media(max-width:768px){
  .calls-panel{display:none}
  .main{flex-direction:column}
  .sidebar{width:100%;border-left:none;border-top:1px solid var(--border);max-height:45vh}
  .chat-win{min-height:300px}
  header{height:auto;padding:8px 10px;flex-wrap:wrap;gap:6px}
  .hdr-mid{gap:4px;max-width:100%}
  .btn-hdr{padding:5px 8px;font-size:11px}
  .stats{gap:3px}
  .stat{padding:3px 7px;font-size:10px}
}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="logo-icon">🔧</div>מרכז שירות</div>
  <div class="hdr-mid">
    <input class="search-box" id="search" placeholder="🔍 חפש מספר או טקסט..." oninput="load()">
    <button class="btn-hdr btn-global on" id="global-btn" onclick="toggleGlobal()">🟢 מענה פעיל</button>
    <button class="btn-hdr btn-notify personal" id="notify-btn" onclick="toggleNotify()" title="שנה יעד קריאות">📨 מורן אישי</button>
    <button class="btn-hdr btn-green" onclick="syncChats()">🔄 סנכרן</button>
    <button class="btn-hdr btn-green" onclick="enableAll()">⚡ לכולם</button>
    <button class="btn-hdr btn-red" onclick="disableAll()">⏸ כבה</button>
    <button class="btn-hdr" style="background:var(--s2);color:var(--muted);border:1px solid var(--border)" onclick="openSettings()">⚙️ הגדרות</button>
  </div>
  <div class="stats">
    <div class="stat">שיחות <b id="s1">0</b></div>
    <div class="stat">קריאות <b id="s2">0</b></div>
  </div>
</header>
<div class="main">
  <div class="calls-panel">
    <div class="cp-head" style="display:flex;align-items:center;justify-content:space-between">
      <span>קריאות שירות</span>
      <button onclick="clearAllCalls()" style="background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger);border-radius:5px;padding:3px 8px;cursor:pointer;font-size:10px">מחק הכל</button>
    </div>
    <div class="calls-list" id="calls-list"><div class="no-items">אין קריאות</div></div>
  </div>
  <div class="chat-win" id="win">
    <div class="empty"><div class="empty-icon">💬</div><div>בחר שיחה</div></div>
  </div>
  <div class="sidebar">
    <div class="sb-head">
      <span class="sb-title">לקוחות</span>
      <button class="btn-add-contact" onclick="openAddContact()">+ הוסף</button>
    </div>
    <div class="notify-info" id="notify-info">קריאות → מורן אישי</div>
    <div class="chat-list" id="list"><div class="no-items">ממתין...</div></div>
  </div>
</div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <h3>📞 פנה ללקוח חדש</h3>
    <input id="contact-phone" placeholder="05XXXXXXXX" type="tel">
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeModal()">ביטול</button>
      <button class="btn-confirm" onclick="addContact()">הוסף לפאנל</button>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="modal-bg" id="settings-modal">
  <div class="settings-modal">
    <h3>⚙️ הגדרות</h3>
    <div class="setting-group">
      <label class="setting-label">📞 מספר טלפון להתראות אישיות (מורן)</label>
      <input class="setting-input" id="s-notify-phone" type="tel" placeholder="972527066110">
    </div>
    <div class="setting-group">
      <label class="setting-label">👥 מזהה קבוצת וואטסאפ להתראות</label>
      <input class="setting-input" id="s-notify-group" placeholder="972529532110-1614167768@g.us">
    </div>
    <div class="setting-group">
      <label class="setting-label">🔑 מספר BOSS (מספר פרטי שלך)</label>
      <input class="setting-input" id="s-boss-phone" type="tel" placeholder="0502580803">
    </div>
    <div class="setting-group">
      <label class="setting-label">🔗 Webhook URL לפתיחת קריאות (אופציונלי)</label>
      <input class="setting-input" id="s-webhook-url" placeholder="https://your-system.com/api/calls">
      <div class="setting-hint">כל קריאה חדשה תשלח POST לכתובת זו</div>
    </div>
    <div class="setting-group">
      <label class="setting-label">📋 Headers ל-Webhook (שורה לכל header)</label>
      <textarea class="setting-textarea" id="s-webhook-headers" placeholder="Authorization: Bearer TOKEN"></textarea>
    </div>
    <div class="setting-group">
      <label class="setting-label">📧 כתובות מייל להתראות (שורה לכל מייל)</label>
      <textarea class="setting-textarea" id="s-emails" placeholder="roi@example.com"></textarea>
      <div class="setting-hint">ידרוש הגדרת RESEND_API_KEY ב-Render</div>
    </div>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeSettings()">ביטול</button>
      <button class="btn-confirm" onclick="saveSettings()">שמור</button>
    </div>
  </div>
</div>

<script>
let chats=[], calls=[], sel=null, globalOn=true, notifyGroup=false;
const TYPE_ICONS={"image":"📷","audio":"🎤","video":"🎬","document":"📄","sticker":"😀","text":""};
function api(u,o){return fetch(u,Object.assign({},o||{},{credentials:'include'}));}

const historyCache={};
async function load(){
  const q=document.getElementById('search').value;
  let d;
  try{
    const r=await api('/api/status'+(q?'?q='+encodeURIComponent(q):''));
    d=await r.json();
  }catch(e){return;}
  // שמור history מה-cache הקיים
  d.chats.forEach(c=>{
    if(historyCache[c.phone]) c.history=historyCache[c.phone];
  });
  chats=d.chats; calls=d.calls; globalOn=d.global_bot_on; notifyGroup=d.notify_to_group;
  {const gs=d;
  
  const btn=document.getElementById('global-btn');
  if(globalOn){btn.className='btn-hdr btn-global on';btn.textContent='🟢 מענה פעיל';}
  else{btn.className='btn-hdr btn-global off';btn.textContent='🔴 מענה כבוי';}
  
  const nb=document.getElementById('notify-btn');
  const ni=document.getElementById('notify-info');
  if(notifyGroup){
    nb.className='btn-hdr btn-notify group';nb.textContent='👥 קבוצה';
    ni.textContent='קריאות → קבוצה וואטסאפ';
  } else {
    nb.className='btn-hdr btn-notify personal';nb.textContent='📨 מורן אישי';
    ni.textContent='קריאות → אישי';
  }
  
  document.getElementById('s1').textContent=chats.length;
  document.getElementById('s2').textContent=calls.length;
  } // end gs block
  renderList(); renderCalls();
  if(sel){
    const c=chats.find(c=>c.phone===sel);
    if(c){
      const prevCount=c.history?c.history.length:0;
      if(!c.history || c.msg_count>prevCount){
        api('/api/chat-history/'+sel).then(r=>r.json()).then(h=>{
          c.history=h;historyCache[sel]=h;renderWin(c);
        });
      } else {
        renderWin(c); // תמיד רנדר כדי שה-topbar יופיע
      }
    } else {
      // המספר לא ב-chats — עדיין הצג את החלון
      const phantom={phone:sel,bot_active:false,msg_count:0,history:historyCache[sel]||[]};
      renderWin(phantom);
    }
  }
}

function renderList(){
  const el=document.getElementById('list');
  if(!chats.length){el.innerHTML='<div class="no-items">ממתין להודעות נכנסות...</div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="ci${c.phone===sel?' active':''}" onclick="pick('${c.phone}')">
      <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
      <div class="ci-info">
        <div class="ci-phone">${fmt(c.phone)}</div>
        <div class="ci-last">${c.last_message?(TYPE_ICONS[c.last_message.type]||'')+' '+esc(c.last_message.message).substring(0,32):'ממתין...'}</div>
      </div>
      <div onclick="event.stopPropagation()">
        <label class="tgl"><input type="checkbox"${c.bot_active?' checked':''} onclick="tog('${c.phone}')"><span class="tsl"></span></label>
      </div>
    </div>`).join('');
}

function renderCalls(){
  const el=document.getElementById('calls-list');
  if(!calls.length){el.innerHTML='<div class="no-items">אין קריאות עדיין</div>';return;}
  el.innerHTML=[...calls].reverse().map(c=>`
    <div class="call-card">
      <div class="call-id">${c.opened_at}</div>
      <div class="call-name">👤 ${esc(c.name)}</div>
      <div class="call-type">🔧 ${esc(c.call_type)}</div>
      <div class="call-row">📞 <span>${esc(c.contact_phone)}</span></div>
      <div class="call-row">📍 <span>${esc(c.address)}</span></div>
      <div class="call-row">📝 <span>${esc(c.description)}</span></div>
      <div style="display:flex;gap:6px;margin-top:6px;align-items:center">
        <select class="status-sel" style="flex:1;margin-top:0" onchange="updateStatus(${c.id},this.value)">
          <option${c.status==='ממתינה לטיפול'?' selected':''}>ממתינה לטיפול</option>
          <option${c.status==='בטיפול'?' selected':''}>בטיפול</option>
          <option${c.status==='הושלמה'?' selected':''}>הושלמה</option>
          <option${c.status==='בוטלה'?' selected':''}>בוטלה</option>
        </select>
        <button onclick="deleteCall(${c.id})" style="background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger);border-radius:5px;padding:4px 8px;cursor:pointer;font-size:11px;white-space:nowrap">🗑 מחק</button>
      </div>
    </div>`).join('');
}

function renderWin(c){
  const h=c.history||[];
  const isActive=c.bot_active&&globalOn;
  // שמור מיקום גלילה לפני רענון
  const msgsEl=document.getElementById('msgs');
  const prevScrollTop = msgsEl ? msgsEl.scrollTop : null;
  const prevScrollHeight = msgsEl ? msgsEl.scrollHeight : null;
  const prevMsgCount = msgsEl ? msgsEl.querySelectorAll('.msg').length : 0;
  document.getElementById('win').innerHTML=`
    <div class="topbar">
      <div class="tb-left">
        <div style="font-size:19px">👤</div>
        <div><div class="tb-phone">${fmt(c.phone)}</div><div class="tb-sub">${c.msg_count||h.length} הודעות</div></div>
      </div>
      <div class="tb-right">
        <span class="badge${isActive?' on':''}">${isActive?'🤖 פעיל':'⏸ כבוי'}</span>
        <button class="btn-sm btn-resend" onclick="sendGreeting('${c.phone}')" title="שלח הודעת פתיחה">👋</button>
        <a class="btn-sm btn-resend" href="https://wa.me/${c.phone.replace(/^972/,'972')}" target="_blank" title="הצטרף לשיחה">💬</a>
        <button class="btn-sm btn-resend" onclick="resendLast('${c.phone}')" title="שלח שוב">🔄</button>
        ${c.bot_active
          ?`<button class="btn-sm btn-deact" onclick="tog('${c.phone}')">⏸ כבה</button>`
          :`<button class="btn-sm btn-act" onclick="tog('${c.phone}')">▶ הפעל</button>`}
      </div>
    </div>
    <div class="messages" id="msgs">
      ${h.length?h.map(m=>`
        <div class="msg ${m.sender}">
          ${m.type&&m.type!=='text'?'<span>'+TYPE_ICONS[m.type]+'</span> ':''}${esc(m.message)}
          <div class="msg-meta">${m.sender==='bot'?'🤖 ':''}${m.time}</div>
        </div>`).join('')
        :'<div style="text-align:center;color:var(--muted);font-size:12px;margin-top:30px">אין הודעות</div>'}
    </div>`;
  const msgs=document.getElementById('msgs');
  if(!msgs) return;
  const newMsgCount = msgs.querySelectorAll('.msg').length;
  const hasNewMessages = newMsgCount > prevMsgCount;
  if(prevScrollTop === null || hasNewMessages){
    // שיחה נפתחה עכשיו או יש הודעה חדשה — גלול למטה
    msgs.scrollTop=msgs.scrollHeight;
  } else {
    // המשתמש גולל — שמור מיקום
    const distFromBottom = prevScrollHeight - prevScrollTop;
    msgs.scrollTop = msgs.scrollHeight - distFromBottom;
  }
}

async function pick(phone){
  sel=phone;
  renderList(); // סמן active מיד
  let c=chats.find(c=>c.phone===phone);
  if(!c){
    // מספר חדש שאין לו עדיין בchats — צור אובייקט בסיסי
    c={phone:phone,bot_active:false,msg_count:0,history:[]};
    chats.push(c);
  }
  // תמיד טען היסטוריה עדכנית
  try{
    const r=await api('/api/chat-history/'+phone);
    c.history=await r.json();
    historyCache[phone]=c.history;
  }catch(e){c.history=[];}
  renderWin(c);
  renderList();
}
const _toggling=new Set();
async function tog(phone){
  if(_toggling.has(phone))return;
  _toggling.add(phone);
  try{
    const r=await api('/api/toggle/'+phone,{method:'POST'});
    const d=await r.json();
    const c=chats.find(c=>c.phone===phone);
    if(c) c.bot_active=d.bot_active;
    renderList();
    if(sel===phone) renderWin(c);
    await load();
  }finally{
    setTimeout(()=>_toggling.delete(phone),1000);
  }
}
async function toggleGlobal(){await api('/api/global-toggle',{method:'POST'});await load();}
async function toggleNotify(){await api('/api/notify-toggle',{method:'POST'});await load();}
async function syncChats(){
  const r=await api('/api/sync-chats',{method:'POST'});
  const d=await r.json();
  if(d.ok){await load();alert('סונכרנו '+d.synced+' שיחות');}
  else alert('שגיאה: '+(d.error||''));
}
async function enableAll(){await api('/api/enable-all',{method:'POST'});await load();}
async function disableAll(){await api('/api/disable-all',{method:'POST'});await load();}
async function resendLast(phone){
  const r=await api('/api/resend-last/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('אין הודעה לשליחה חוזרת');
  else await load();
}
async function sendGreeting(phone){
  const r=await api('/api/send-greeting/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('שגיאה בשליחה');
  else await load();
}
async function updateStatus(id,status){
  await api('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function openAddContact(){document.getElementById('modal').classList.add('open');document.getElementById('contact-phone').focus();}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('contact-phone').value='';}
async function addContact(){
  const phone=document.getElementById('contact-phone').value.trim();
  if(!phone){alert('הזן מספר');return;}
  const r=await api('/api/add-contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
  const d=await r.json();
  if(d.ok){closeModal();await load();pick(d.phone);}
  else alert(d.error||'שגיאה');
}
function fmt(p){return String(p).replace('@c.us','').replace(/^972/,'0');}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function deleteCall(id){
  if(!confirm('למחוק קריאה זו?'))return;
  await api('/api/service-calls/'+id,{method:'DELETE'});
  await load();
}
async function clearAllCalls(){
  if(!confirm('למחוק את כל הקריאות?'))return;
  await api('/api/service-calls/clear',{method:'POST'});
  await load();
}
async function openSettings(){
  const r=await api('/api/settings');
  const s=await r.json();
  document.getElementById('s-notify-phone').value=s.notify_personal_phone||'';
  document.getElementById('s-notify-group').value=s.notify_group_id||'';
  document.getElementById('s-boss-phone').value=s.boss_phone||'';
  document.getElementById('s-webhook-url').value=s.webhook_url||'';
  document.getElementById('s-webhook-headers').value=s.webhook_headers||'';
  document.getElementById('s-emails').value=(s.notification_emails||[]).join(String.fromCharCode(10));
  document.getElementById('settings-modal').classList.add('open');
}
function closeSettings(){document.getElementById('settings-modal').classList.remove('open');}
async function saveSettings(){
  const emails=document.getElementById('s-emails').value.trim().split(String.fromCharCode(10)).map(e=>e.trim()).filter(Boolean);
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    notify_personal_phone:document.getElementById('s-notify-phone').value.trim(),
    notify_group_id:document.getElementById('s-notify-group').value.trim(),
    boss_phone:document.getElementById('s-boss-phone').value.trim(),
    webhook_url:document.getElementById('s-webhook-url').value.trim(),
    webhook_headers:document.getElementById('s-webhook-headers').value.trim(),
    notification_emails:emails
  })});
  closeSettings();
  alert('ההגדרות נשמרו');
  await load();
}
load();setInterval(load,5000);
</script>
</body>
</html>"""


MOBILE = r"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>מרכז שירות</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;600;700;900&display=swap');
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{--bg:#0b0d12;--s1:#13161f;--s2:#1a1e2a;--s3:#222736;--border:#252b3b;--accent:#25d366;--text:#dde1ec;--muted:#5a6378;--danger:#e74c3c;}
html,body{height:100%;overflow:hidden}
body{font-family:'Heebo',sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}
.hdr{background:var(--s1);border-bottom:1px solid var(--border);padding:10px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.hdr-icon{width:30px;height:30px;background:linear-gradient(135deg,#25d366,#128c7e);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.hdr-title{font-weight:800;font-size:15px;flex:1}
.btn-hm{border:none;border-radius:16px;padding:5px 11px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.btn-notify.group{background:rgba(52,152,219,.2);color:#3498db;border:1px solid #3498db}
.btn-notify.personal{background:rgba(155,89,182,.2);color:#9b59b6;border:1px solid #9b59b6}
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--border);flex-shrink:0}
.tab{flex:1;padding:11px;text-align:center;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.search-wrap{padding:8px 12px;background:var(--bg);border-bottom:1px solid var(--border);flex-shrink:0}
.search-input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:9px 13px;color:var(--text);font-family:inherit;font-size:15px;outline:none}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:var(--muted)}
.pages{flex:1;overflow:hidden;display:flex;flex-direction:column}
.page{display:none;flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;flex-direction:column}
.page.active{display:flex}
.add-btn-wrap{padding:10px 12px 4px}
.btn-add-full{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:12px;padding:11px;font-family:inherit;font-size:14px;font-weight:600;color:var(--accent);cursor:pointer;text-align:center}
.cards{padding:8px 12px 80px;display:flex;flex-direction:column;gap:8px}
.card{background:var(--s1);border:1px solid var(--border);border-radius:13px;overflow:hidden}
.card.on{border-color:rgba(37,211,102,.35)}
.card-top{padding:12px 13px;display:flex;align-items:center;gap:10px}
.av{width:38px;height:38px;border-radius:50%;background:var(--s2);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;position:relative}
.dot{position:absolute;bottom:-1px;left:-1px;width:12px;height:12px;border-radius:50%;border:2px solid var(--s1);background:var(--muted)}
.dot.on{background:var(--accent);box-shadow:0 0 6px var(--accent)}
.ci{flex:1;min-width:0}
.ci-phone{font-weight:700;font-size:14px;margin-bottom:2px}
.ci-last{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tgl{position:relative;width:38px;height:21px;display:inline-block;flex-shrink:0}
.tgl input{opacity:0;width:0;height:0}
.tsl{position:absolute;cursor:pointer;inset:0;background:var(--border);border-radius:21px;transition:.25s}
.tsl:before{content:"";position:absolute;height:15px;width:15px;right:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
input:checked+.tsl{background:var(--accent)}
input:checked+.tsl:before{transform:translateX(-17px)}
.card-btns{border-top:1px solid var(--border);display:flex}
.card-btn{flex:1;background:none;border:none;padding:9px;font-family:inherit;font-size:12px;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;gap:5px}
.card-btn:not(:last-child){border-left:1px solid var(--border)}
.card-btn:active{background:var(--s2)}
.call-cards{padding:8px 12px 80px}
.call-card{background:var(--s1);border:1px solid var(--border);border-radius:13px;padding:13px;margin-bottom:9px;font-size:12px}
.call-id{font-size:10px;color:var(--muted);margin-bottom:4px}
.call-name{font-weight:700;font-size:14px;margin-bottom:3px}
.call-type{color:var(--accent);font-size:11px;margin-bottom:7px}
.call-row{color:var(--muted);margin-bottom:3px}
.call-row span{color:var(--text)}
.status-sel{margin-top:9px;width:100%;background:var(--s3);border:1px solid var(--border);border-radius:9px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none;cursor:pointer}
.chat-view{display:none;position:fixed;inset:0;background:var(--bg);flex-direction:column;z-index:200}
.chat-view.open{display:flex}
.chat-hdr{padding:11px 14px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px;flex-shrink:0}
.back-btn{background:none;border:none;color:var(--accent);font-size:24px;cursor:pointer;padding:0 4px;line-height:1}
.chat-hdr-info{flex:1;min-width:0}
.chat-hdr-phone{font-weight:700;font-size:15px}
.chat-hdr-sub{font-size:10px;color:var(--muted)}
.chat-hdr-btns{display:flex;gap:6px;align-items:center}
.btn-xs{border:none;border-radius:7px;padding:5px 10px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer}
.btn-act{background:var(--accent);color:#000}
.btn-deact{background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:6px;-webkit-overflow-scrolling:touch}
.msg{max-width:82%;padding:8px 11px;border-radius:11px;font-size:13px;line-height:1.5;white-space:pre-wrap}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.2);align-self:flex-start;border-bottom-left-radius:3px}
.msg-time{font-size:9px;color:var(--muted);margin-top:2px}
.msg.client .msg-time{text-align:right}
.empty{padding:40px 20px;text-align:center;color:var(--muted)}
.empty-icon{font-size:40px;opacity:.3;margin-bottom:8px}
.badge{padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700;background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.badge.on{background:rgba(37,211,102,.12);color:var(--accent);border-color:rgba(37,211,102,.4)}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;align-items:flex-end}
.modal-bg.open{display:flex}
.modal{background:var(--s1);border-top:1px solid var(--border);border-radius:20px 20px 0 0;padding:20px;width:100%}
.modal h3{margin-bottom:14px;font-size:16px}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:12px;color:var(--text);font-family:inherit;font-size:15px;outline:none;margin-bottom:12px;direction:ltr}
.modal input:focus{border-color:var(--accent)}
.modal-btns{display:flex;gap:8px}
.btn-cancel{flex:1;background:var(--s2);color:var(--muted);border:1px solid var(--border);border-radius:10px;padding:12px;font-family:inherit;font-size:14px;cursor:pointer}
.btn-confirm{flex:2;background:var(--accent);color:#000;border:none;border-radius:10px;padding:12px;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-icon">🔧</div>
  <div class="hdr-title">מרכז שירות</div>
  <button class="btn-hm btn-global on" id="g-btn" onclick="toggleGlobal()">🟢</button>
  <button class="btn-hm btn-notify personal" id="n-btn" onclick="toggleNotify()">📨</button>
  <button class="btn-hm" style="background:rgba(37,211,102,.2);color:var(--accent);border:1px solid var(--accent)" onclick="syncChats()">🔄</button>
  <button class="btn-hm" style="background:rgba(37,211,102,.2);color:var(--accent);border:1px solid var(--accent)" onclick="enableAll()">⚡</button>
</div>
<div class="tabs">
  <div class="tab active" id="tab-clients" onclick="showTab('clients')">👥 לקוחות</div>
  <div class="tab" id="tab-calls" onclick="showTab('calls')">🔧 קריאות <span id="calls-badge"></span></div>
</div>
<div class="pages">
  <div class="page active" id="page-clients">
    <div class="search-wrap" style="padding:8px 12px 4px">
      <input class="search-input" id="search" placeholder="🔍 חפש מספר..." oninput="load()" autocomplete="off">
    </div>
    <div class="add-btn-wrap">
      <button class="btn-add-full" onclick="openModal()">+ הוסף לקוח חדש</button>
    </div>
    <div class="cards" id="cards"></div>
  </div>
  <div class="page" id="page-calls">
    <div class="call-cards" id="call-cards"></div>
  </div>
</div>
<div class="chat-view" id="chat-view">
  <div class="chat-hdr">
    <button class="back-btn" onclick="closeChat()">‹</button>
    <div class="chat-hdr-info">
      <div class="chat-hdr-phone" id="cv-phone"></div>
      <div class="chat-hdr-sub" id="cv-sub"></div>
    </div>
    <div class="chat-hdr-btns">
      <span class="badge" id="cv-badge">כבוי</span>
      <label class="tgl"><input type="checkbox" id="cv-tgl" onchange="cvToggle()"><span class="tsl"></span></label>
    </div>
  </div>
  <div class="msgs" id="cv-msgs"></div>
</div>
<div class="modal-bg" id="modal">
  <div class="modal">
    <h3>📞 לקוח חדש</h3>
    <input id="m-phone" placeholder="05XXXXXXXX" type="tel" inputmode="numeric">
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeModal()">ביטול</button>
      <button class="btn-confirm" onclick="addContact()">הוסף</button>
    </div>
  </div>
</div>
<script>
let chats=[],calls=[],cvPhone=null,globalOn=true,notifyGroup=false;
const ICONS={"image":"📷","audio":"🎤","video":"🎬","document":"📄","sticker":"😀","text":""};
function api(u,o){return fetch(u,Object.assign({credentials:'include'},o||{}));}
async function load(){
  const q=(document.getElementById('search').value||'').trim();
  try{
    const [cr,sr,gr]=await Promise.all([
      api('/api/chats'+(q?'?q='+encodeURIComponent(q):'')),
      api('/api/service-calls'),api('/api/global-status')]);
    chats=await cr.json();calls=await sr.json();const gs=await gr.json();
    globalOn=gs.global_bot_on;notifyGroup=gs.notify_to_group;
    const gb=document.getElementById('g-btn');
    if(globalOn){gb.className='btn-hm btn-global on';gb.textContent='🟢';}
    else{gb.className='btn-hm btn-global off';gb.textContent='🔴';}
    const nb=document.getElementById('n-btn');
    if(notifyGroup){nb.className='btn-hm btn-notify group';nb.textContent='👥';}
    else{nb.className='btn-hm btn-notify personal';nb.textContent='📨';}
    document.getElementById('calls-badge').textContent=calls.length?` (${calls.length})`:'';
    renderCards();renderCalls();
    if(cvPhone){const c=chats.find(x=>x.phone===cvPhone);if(c)updateCV(c);}
  }catch(e){console.error(e);}
}
function showTab(t){
  ['clients','calls'].forEach(x=>{
    document.getElementById('tab-'+x).classList.toggle('active',x===t);
    document.getElementById('page-'+x).classList.toggle('active',x===t);
  });
}
function renderCards(){
  const el=document.getElementById('cards');
  if(!chats.length){el.innerHTML='<div class="empty"><div class="empty-icon">💬</div><div>אין שיחות</div></div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="card${c.bot_active?' on':''}">
      <div class="card-top">
        <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
        <div class="ci">
          <div class="ci-phone">${fmt(c.phone)}</div>
          <div class="ci-last">${c.last_message?(ICONS[c.last_message.type]||'')+' '+esc(c.last_message.message).substring(0,38):'ממתין...'}</div>
        </div>
        <label class="tgl" onclick="event.stopPropagation()">
          <input type="checkbox"${c.bot_active?' checked':''} onclick="tog('${c.phone}')"><span class="tsl"></span>
        </label>
      </div>
      <div class="card-btns">
        <button class="card-btn" onclick="openChat('${c.phone}')">💬 ${(c.history||[]).length}</button>
        <button class="card-btn" onclick="resendFor('${c.phone}')">🔄 שוב</button>
      </div>
    </div>`).join('');
}
function renderCalls(){
  const el=document.getElementById('call-cards');
  if(!calls.length){el.innerHTML='<div class="empty"><div class="empty-icon">🔧</div><div>אין קריאות</div></div>';return;}
  el.innerHTML=[...calls].reverse().map(c=>`
    <div class="call-card">
      <div class="call-id">#${c.id} — ${c.opened_at}</div>
      <div class="call-name">👤 ${esc(c.name)}</div>
      <div class="call-type">🔧 ${esc(c.call_type)}</div>
      <div class="call-row">📞 <span>${esc(c.contact_phone)}</span></div>
      <div class="call-row">📍 <span>${esc(c.address)}</span></div>
      <div class="call-row">📝 <span>${esc(c.description)}</span></div>
      <select class="status-sel" onchange="updateStatus(${c.id},this.value)">
        <option${c.status==='ממתינה לטיפול'?' selected':''}>ממתינה לטיפול</option>
        <option${c.status==='בטיפול'?' selected':''}>בטיפול</option>
        <option${c.status==='הושלמה'?' selected':''}>הושלמה</option>
        <option${c.status==='בוטלה'?' selected':''}>בוטלה</option>
      </select>
    </div>`).join('');
}
function openChat(phone){
  cvPhone=phone;
  const c=chats.find(x=>x.phone===phone);
  if(c)updateCV(c);
  document.getElementById('chat-view').classList.add('open');
}
function updateCV(c){
  document.getElementById('cv-phone').textContent=fmt(c.phone);
  document.getElementById('cv-sub').textContent=(c.history||[]).length+' הודעות';
  document.getElementById('cv-tgl').checked=!!c.bot_active;
  const badge=document.getElementById('cv-badge');
  const active=c.bot_active&&globalOn;
  badge.className='badge'+(active?' on':'');
  badge.textContent=active?'🤖 פעיל':'⏸ כבוי';
  const msgs=document.getElementById('cv-msgs');
  const atBottom=msgs.scrollHeight-msgs.scrollTop-msgs.clientHeight<80;
  const h=c.history||[];
  msgs.innerHTML=h.length?h.map(m=>`
    <div class="msg ${m.sender}">
      ${m.type&&m.type!=='text'?(ICONS[m.type]||'')+' ':''}${esc(m.message)}
      <div class="msg-time">${m.sender==='bot'?'🤖 ':''}${m.time}</div>
    </div>`).join('')
    :'<div class="empty"><div class="empty-icon">💬</div><div>אין הודעות</div></div>';
  if(atBottom)msgs.scrollTop=msgs.scrollHeight;
}
function closeChat(){document.getElementById('chat-view').classList.remove('open');cvPhone=null;}
async function cvToggle(){
  if(cvPhone){
    const r=await api('/api/toggle/'+cvPhone,{method:'POST'});
    const d=await r.json();
    const c=chats.find(x=>x.phone===cvPhone);
    if(c) c.bot_active=d.bot_active;
    renderCards();
    if(c) updateCV(c);
    await load();
  }
}
const _toggling=new Set();
async function tog(phone){
  if(_toggling.has(phone))return;
  _toggling.add(phone);
  try{
    const r=await api('/api/toggle/'+phone,{method:'POST'});
    const d=await r.json();
    const c=chats.find(c=>c.phone===phone);
    if(c) c.bot_active=d.bot_active;
    renderList();
    if(sel===phone) renderWin(c);
    await load();
  }finally{
    setTimeout(()=>_toggling.delete(phone),1000);
  }
}
async function toggleGlobal(){await api('/api/global-toggle',{method:'POST'});await load();}
async function toggleNotify(){await api('/api/notify-toggle',{method:'POST'});await load();}
async function syncChats(){
  const r=await api('/api/sync-chats',{method:'POST'});const d=await r.json();
  alert(d.ok?'סונכרנו '+d.synced+' שיחות':'שגיאה: '+(d.error||''));
  if(d.ok)await load();
}
async function enableAll(){await api('/api/enable-all',{method:'POST'});await load();}
async function resendFor(phone){
  const r=await api('/api/resend-last/'+phone,{method:'POST'});const d=await r.json();
  if(!d.ok)alert('אין הודעה');else await load();
}
async function updateStatus(id,status){
  await api('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function openModal(){document.getElementById('modal').classList.add('open');setTimeout(()=>document.getElementById('m-phone').focus(),200);}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('m-phone').value='';}
async function addContact(){
  const phone=document.getElementById('m-phone').value.trim();
  if(!phone){alert('הזן מספר');return;}
  const r=await api('/api/add-contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
  const d=await r.json();
  if(d.ok){closeModal();await load();openChat(d.phone);}else alert(d.error||'שגיאה');
}
function fmt(p){return String(p||'').replace('@c.us','').replace(/^972/,'0');}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
load();setInterval(load,5000);
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    html = DASHBOARD.replace("{{PORTAL_ACCENT}}", PORTAL_ACCENT).replace("{{PORTAL_BG}}", PORTAL_BG)
    return render_template_string(html)

@app.route("/mobile")
def mobile():
    return render_template_string(MOBILE)

@app.route("/ping")
def ping():
    return "ok"

# ─── Polling ──────────────────────────────────────────────────
def polling_loop():
    if not GREEN_API_URL or not GREEN_API_TOKEN:
        print("[Polling] מנוטרל — חסרים GREEN_API_INSTANCE / GREEN_API_TOKEN", flush=True)
        return
    url_receive = f"{GREEN_API_URL}/receiveNotification/{GREEN_API_TOKEN}"
    url_delete  = f"{GREEN_API_URL}/deleteNotification/{GREEN_API_TOKEN}"
    while True:
        try:
            r = requests.get(url_receive, timeout=10)
            if r.status_code == 200 and r.text and r.text != "null":
                data = r.json()
                if data:
                    receipt_id = data.get("receiptId")
                    body = data.get("body", {})
                    process_green_event(body, receipt_id)
                    if receipt_id:
                        requests.delete(f"{url_delete}/{receipt_id}", timeout=5)
        except Exception as e:
            print(f"[Polling] error: {e}", flush=True)
        time.sleep(3)

def _keep_alive_loop():
    while True:
        try:
            if KEEP_ALIVE_URL:
                requests.get(KEEP_ALIVE_URL, timeout=8)
        except Exception:
            pass
        time.sleep(240)

if USE_POLLING:
    threading.Thread(target=polling_loop, daemon=True).start()

if ENABLE_KEEP_ALIVE and KEEP_ALIVE_URL:
    threading.Thread(target=_keep_alive_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=FLASK_DEBUG, host="0.0.0.0", port=FLASK_PORT)
