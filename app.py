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
    print("[Auth] FLASK_SECRET_KEY לא הוגדר — נוצר מפתח אקראי (סשנים יאבדו אחרי ריסטארט). הגדר משתנה סביבה.", flush=True)
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on"):
    app.config["SESSION_COOKIE_SECURE"] = True

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
AUTH_CONFIGURED = bool(ADMIN_PASSWORD or ADMIN_TOKEN)
if not AUTH_CONFIGURED:
    print("[Auth] לא הוגדרו ADMIN_PASSWORD / ADMIN_TOKEN — הפאנל וה-API פתוחים לכולם. מומלץ להגדיר מיד.", flush=True)

GREEN_API_INSTANCE   = os.environ.get("GREEN_API_INSTANCE", "").strip()
GREEN_API_TOKEN      = os.environ.get("GREEN_API_TOKEN", "").strip()
GREEN_API_HOST       = os.environ.get("GREEN_API_HOST", "https://api.green-api.com").rstrip("/")
GREEN_API_URL        = f"{GREEN_API_HOST}/waInstance{GREEN_API_INSTANCE}" if GREEN_API_INSTANCE else ""
NOTIFY_PHONE         = os.environ.get("NOTIFY_PHONE", "").strip()
BOSS_PHONE           = os.environ.get("BOSS_PHONE", "").strip()
BUSINESS_NAME        = "שירות לקוחות"
GREETING_MSG         = "היי! איך אפשר לעזור? 😊"
KEEP_ALIVE_URL       = os.environ.get("KEEP_ALIVE_URL", "").strip()
ENABLE_KEEP_ALIVE    = os.environ.get("ENABLE_KEEP_ALIVE", "false").strip().lower() in ("1", "true", "yes", "on")
# כמו בגרסה המקורית: polling דולק כברירת מחדל. אם יש רק webhook — הגדר USE_POLLING=false
USE_POLLING          = os.environ.get("USE_POLLING", "true").strip().lower() in ("1", "true", "yes", "on")
# הודעה נכנסת ללקוח חדש: האם להפעיל בוט אוטומטית (כמו בקוד הישן). false = רק מהפאנל
AUTO_BOT_NEW_CHATS   = os.environ.get("AUTO_BOT_NEW_CHATS", "true").strip().lower() in ("1", "true", "yes", "on")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "").strip()
FLASK_DEBUG          = os.environ.get("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")
# Render/Heroku מגדירים PORT; אסור שהאפליקציה תתרסק אם הערך ריק
_raw_port = (os.environ.get("PORT") or os.environ.get("FLASK_PORT") or "5000").strip()
try:
    FLASK_PORT = int(_raw_port)
except ValueError:
    FLASK_PORT = 5000

state_lock = threading.RLock()
_seen_event_keys = {}
_seen_lock = threading.Lock()
MAX_SEEN_KEYS = 8000
ANTHROPIC_KEY        = os.environ.get("ANTHROPIC_KEY", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_URL       = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
google_tokens        = {}
google_contacts      = []  # רשימת אנשי קשר מגוגל
CLAUDE_API_URL       = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL         = "claude-sonnet-4-20250514"

# ─── נתונים ───────────────────────────────────────────────────
sessions      = {}
service_calls = []
bot_enabled   = {}
chat_history  = {}
greeting_sent = {}
global_bot_on = True
last_bot_msg_time = {}   # phone -> timestamp of last bot message
reminder_timers   = {}   # phone -> timer thread

DATA_FILE = "data.json"

def save_data():
    with state_lock:
        payload = {
            "sessions": sessions,
            "service_calls": service_calls,
            "bot_enabled": bot_enabled,
            "chat_history": chat_history,
            "greeting_sent": greeting_sent,
            "global_bot_on": global_bot_on
        }
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Save] error: {e}", flush=True)

def _migrate_history_ts():
    base = datetime.now(timezone.utc).replace(microsecond=0)
    for phone, msgs in chat_history.items():
        for i, m in enumerate(msgs or []):
            if not isinstance(m, dict):
                continue
            if m.get("ts"):
                continue
            m["ts"] = (base.replace(second=min(base.second + i, 59))).isoformat()

def load_data():
    global sessions, service_calls, bot_enabled, chat_history, greeting_sent, global_bot_on
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            with state_lock:
                sessions      = d.get("sessions", {})
                service_calls = d.get("service_calls", [])
                bot_enabled   = d.get("bot_enabled", {})
                chat_history  = d.get("chat_history", {})
                greeting_sent = d.get("greeting_sent", {})
                global_bot_on = d.get("global_bot_on", True)
            _migrate_history_ts()
            print("[Load] data loaded successfully", flush=True)
    except Exception as e:
        print(f"[Load] error: {e}", flush=True)

load_data()

def phone972(p):
    p = str(p).replace("@c.us", "").strip()
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
    israel = datetime.now(timezone.utc) + timedelta(hours=3)
    h = israel.hour
    if 5 <= h < 12:
        return "בוקר טוב! 🌅"
    if 12 <= h < 17:
        return "צהריים טובים! ☀️"
    if 17 <= h < 22:
        return "ערב טוב! 🌆"
    return "לילה טוב! 🌙"

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
        return jsonify({"ok": False, "error": "לא מאומת / Unauthorized"}), 401
    nxt = request.path
    if request.query_string:
        nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
    return redirect(url_for("login_page", next=nxt))

def extract_message_id(payload):
    if not isinstance(payload, dict):
        return None
    mid = payload.get("idMessage")
    if mid:
        return str(mid)
    md = payload.get("messageData") or {}
    for k in ("idMessage", "messageId"):
        if md.get(k):
            return str(md[k])
    return None

def is_duplicate_green_event(body, receipt_id):
    mid = extract_message_id(body)
    keys = []
    if mid:
        keys.append(f"m:{mid}")
    if receipt_id is not None:
        keys.append(f"r:{receipt_id}")
    if not keys:
        return False
    now = time.monotonic()
    with _seen_lock:
        for k in keys:
            if k in _seen_event_keys:
                return True
        for k in keys:
            _seen_event_keys[k] = now
        if len(_seen_event_keys) > MAX_SEEN_KEYS:
            for k, _ in sorted(_seen_event_keys.items(), key=lambda x: x[1])[:3000]:
                del _seen_event_keys[k]
    return False

SYSTEM_PROMPT = """אתה גל — עוזר דיגיטלי של רועי, חברת בריכות שחייה אקוופולקו.

זהות:
- שמך גל
- אם שואלים מי אתה: "אני גל, העוזר הדיגיטלי של רועי 😊"
- אל תפתח עם ברכת שעה בתשובות — הברכה כבר נשלחה בהודעה הראשונה

סגנון:
- עברית יומיומית, חמה, נעימה וטבעית
- הודעות קצרות וטבעיות — לא יותר מ-2-3 שורות בכל פעם
- חמים ואכפתי אבל לא מוגזם
- אל תשלח ברכת בוקר/ערב — ההודעה הראשונה כבר כוללת ברכה
- נסה לאסוף את כל הפרטים ב-1-2 שאלות, לא פינג פונג ארוך
- אם הלקוח מתאר תקלה — שאל: "אוי, לא נעים 😕 מה שמך, כתובת הבריכה וטלפון?"
- אם שלח הקלטה קולית או וידאו — הגב: "תודה! 😊 שלח לי גם בטקסט: שמך, כתובת הבריכה וטלפון"

הפרטים שצריך לאסוף:
1. שם
2. כתובת הבריכה (רחוב, מספר, עיר)
3. סוג הפנייה: תקלה/תיקון, תחזוקה, בריכה חדשה, שיפוץ, או אחר
4. תיאור הבעיה או הבקשה
5. טלפון ליצירת קשר

כללים:
- אם שלח תמונה, הקלטה קולית, וידאו — הגב בנימוס והמשך לאסוף פרטים
- אם לא רוצה שירות — סגור בנימוס
- אחרי שיש לך את כל הפרטים — הצג סיכום קצר ובקש אישור
- אחרי אישור — החזר JSON בדיוק כך (ללא טקסט נוסף):
  {"action":"open_call","name":"...","address":"...","call_type":"...","description":"...","contact_phone":"..."}
- אם ביטל — החזר: {"action":"cancelled"}
- אחרת — החזר: {"action":"continue","message":"הודעה ללקוח"}
- אל תציין מספר קריאה בשיחה"""

BOSS_SYSTEM_PROMPT = """אתה גל — עוזר אישי חכם של רועי, בעל חברת בריכות שחייה אקוופולקו.
רועי הוא הבוס שלך. עזור לו בכל דבר — עסקי, אישי, טכני, יצירתי, או כל תחום אחר.

אישיות:
- חכם, ישיר, יעיל
- עברית טבעית וקצרה — רועי עסוק
- עונה על הכל ללא הגבלה — רועי הבוס

כשרועי מבקש לפתוח קריאת שירות:
  {"action":"open_call","name":"...","address":"...","call_type":"...","description":"...","contact_phone":"..."}
כשרועי מבקש לשלוח הודעה:
  {"action":"send_message","phone":"...","message":"..."}
אחרת:
  {"action":"continue","message":"תשובה לרועי"}"""


def parse_green_msg(msg_data):
    msg_type_raw = (msg_data or {}).get("typeMessage", "textMessage")
    type_map = {
        "textMessage":          ("text", lambda d: d.get("textMessageData",{}).get("textMessage","") or d.get("extendedTextMessageData",{}).get("text","")),
        "extendedTextMessage": ("text", lambda d: d.get("extendedTextMessageData",{}).get("text","") or d.get("textMessageData",{}).get("textMessage","")),
        "imageMessage":    ("image",    lambda d: "[שלח תמונה]"),
        "audioMessage":    ("audio",    lambda d: "[שלח הקלטה קולית]"),
        "videoMessage":    ("video",    lambda d: "[שלח וידאו]"),
        "callMessage":     ("text",     lambda d: "[התקשר/ה בשיחת וואטסאפ]"),
        "documentMessage": ("document", lambda d: "[שלח מסמך]"),
        "stickerMessage":  ("sticker",  lambda d: "[שלח סטיקר]"),
        "locationMessage": ("text",     lambda d: "[שיתף מיקום]"),
        "contactMessage":  ("text",     lambda d: "[שיתף איש קשר]"),
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
        print("[GreenAPI] GREEN_API_INSTANCE / GREEN_API_TOKEN לא הוגדרו — לא נשלחה הודעה", flush=True)
        return False
    try:
        url = f"{GREEN_API_URL}/sendMessage/{GREEN_API_TOKEN}"
        chat_id = phone if "@c.us" in phone else f"{phone}@c.us"
        r = requests.post(url, json={"chatId": chat_id, "message": text}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[GreenAPI] error: {e}")
        return False


def add_to_history(phone, sender, message, msg_type="text"):
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "sender": sender, "message": message,
        "time": datetime.now().strftime("%H:%M"),
        "type": msg_type,
        "ts": ts
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
    client_num = str(phone).replace("@c.us","").replace("972","0",1)
    return "\n".join([
        f"🔔 *קריאה חדשה*",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 *שם:* {data.get('name','-')}",
        f"📞 *טלפון:* {data.get('contact_phone','-')}",
        f"📍 *כתובת:* {data.get('address','-')}",
        f"🔧 *סוג:* {data.get('call_type','-')}",
        f"📝 *תיאור:* {data.get('description','-')}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📱 *מספר לקוח:* {client_num}",
        f"🕐 *נפתח:* {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"",
        f"⚡ נא לפתוח קריאה במערכת."
    ])


def ask_claude(history, user_msg, msg_type="text", is_boss=False):
    if not (ANTHROPIC_KEY or "").strip():
        return {"action": "continue", "message": "שירות הבוט לא מוגדר כרגע (חסר מפתח AI). צור קשר עם הנציג."}
    try:
        messages = []
        for h in history[-14:]:
            role = "user" if h["sender"] == "client" else "assistant"
            if h.get("type") in ["image","audio","document","video","sticker"] and h["sender"] == "client":
                content = f"[הלקוח שלח {h.get('type','קובץ')}] {h['message']}"
            else:
                content = h["message"]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{content}"
            else:
                messages.append({"role": role, "content": content})

        if msg_type in ["image","audio","document","video","sticker"]:
            current_msg = f"[הלקוח שלח {msg_type}] {user_msg}"
        else:
            current_msg = user_msg

        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += f"\n{current_msg}"
        else:
            messages.append({"role": "user", "content": current_msg})

        system = BOSS_SYSTEM_PROMPT if is_boss else SYSTEM_PROMPT
        max_tokens = 1000 if is_boss else 600
        resp = requests.post(
            CLAUDE_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages
            },
            timeout=20
        )
        try:
            data = resp.json()
        except Exception:
            print(f"[Claude] bad JSON status={resp.status_code}", flush=True)
            return {"action": "continue", "message": "מצטערים, שגיאת שרת זמנית. נסה שוב."}
        if "content" not in data:
            print(f"[Claude] error payload: {data}", flush=True)
            return {"action": "continue", "message": "מצטערים, לא הצלחתי להשיב כרגע. נסה שוב."}
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
        print(f"[Claude] error: {e}")
        return {"action": "continue", "message": "מצטערים, אירעה שגיאה זמנית. נסה שוב בעוד רגע."}


def transcribe_audio(audio_url):
    if not (ANTHROPIC_KEY or "").strip():
        return None
    try:
        r = requests.get(audio_url, timeout=15)
        if r.status_code != 200:
            return None
        audio_b64 = base64.b64encode(r.content).decode()
        resp = requests.post(
            CLAUDE_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "תמלל את ההקלטה הזו לעברית. החזר רק את הטקסט המתומלל ללא הסברים."},
                        {"type": "document", "source": {"type": "base64", "media_type": "audio/ogg", "data": audio_b64}}
                    ]
                }]
            },
            timeout=30
        )
        data = resp.json()
        if "content" not in data:
            return None
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"[Transcribe] error: {e}", flush=True)
        return None


def cancel_reminder(phone):
    with state_lock:
        t = reminder_timers.pop(phone, None)
    if t:
        t.cancel()


def schedule_reminder(phone, last_msg):
    """שולח תזכורת אחרי 30 שניות אם הלקוח לא ענה"""
    def remind():
        with state_lock:
            on = bot_enabled.get(phone, False) and global_bot_on
        if on:
            send_message(phone, last_msg)
            add_to_history(phone, "bot", f"[תזכורת] {last_msg}")
            save_data()
    t = threading.Timer(30.0, remind)
    t.daemon = True
    with state_lock:
        old = reminder_timers.pop(phone, None)
        if old:
            old.cancel()
        reminder_timers[phone] = t
    t.start()


def handle_message(phone, body, msg_type="text", audio_url=None):
    is_boss = is_boss_phone(phone)
    print(f"[Handle] phone={phone} is_boss={is_boss}", flush=True)

    if msg_type == "audio" and audio_url and is_boss:
        transcribed = transcribe_audio(audio_url)
        if transcribed:
            body = f"[הקלטה קולית — תמלול: {transcribed}]"
            add_to_history(phone, "client", f"🎤 {transcribed}", "audio")

    with state_lock:
        cancel_reminder(phone)
        history = list(chat_history.get(phone, []))

    result = ask_claude(history, body, msg_type, is_boss=is_boss)
    action = result.get("action", "continue")

    if action == "open_call":
        with state_lock:
            cancel_reminder(phone)
            call_id = len(service_calls) + 1
            service_calls.append({
                "id": call_id, "phone": phone,
                "name": result.get("name","-"),
                "address": result.get("address","-"),
                "call_type": result.get("call_type","-"),
                "description": result.get("description","-"),
                "contact_phone": result.get("contact_phone","-"),
                "opened_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
                "status": "ממתינה לטיפול"
            })
            reset_session(phone)
        if NOTIFY_PHONE:
            send_message(NOTIFY_PHONE, build_notify_message(phone, result))
        save_data()
        if is_boss:
            return "✅ הקריאה נפתחה ונשלח עדכון לנציג."
        return (
            f"✅ *הקריאה נפתחה בהצלחה!*\n\n"
            f"נציג יצור איתך קשר בהקדם.\n"
            f"תודה שפנית ל{BUSINESS_NAME}! 🙏\n\n"
            f"לקריאה נוספת — כתוב לי בכל עת 😊"
        )

    if action == "send_message" and is_boss:
        target = result.get("phone", "")
        msg_to_send = result.get("message", "")
        if target and msg_to_send:
            if target.startswith("0"):
                target = "972" + target[1:]
            elif not target.startswith("972"):
                target = "972" + target
            sent = send_message(target, msg_to_send)
            return f"✅ נשלח ל-{target}" if sent else "❌ שגיאה בשליחה"
        return "❌ חסרים פרטים"

    if action == "cancelled":
        with state_lock:
            cancel_reminder(phone)
            reset_session(phone)
        save_data()
        return "בסדר גמור! אם תצטרך עזרה בעתיד — אנחנו כאן. 🙏"

    reply = result.get("message", "לא הבנתי, נסה שוב.")
    if not is_boss:
        schedule_reminder(phone, reply)
    return reply


def process_green_event(body, receipt_id=None):
    """מעבד גוף webhook אחד (גם מ-polling וגם מ-POST /webhook)."""
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
        if not body_text:
            return
        is_boss = is_boss_phone(phone)
        default_bot = is_boss or AUTO_BOT_NEW_CHATS
        audio_url = extract_audio_url(msg_data) if msg_type == "audio" else None
        with state_lock:
            bot_enabled.setdefault(phone, default_bot)
            sessions.setdefault(phone, {"step": "active", "data": {}})
        add_to_history(phone, "client", body_text, msg_type)
        save_data()
        with state_lock:
            allow_reply = bot_enabled.get(phone, False) and global_bot_on
        if allow_reply:
            reply = handle_message(phone, body_text, msg_type, audio_url=audio_url)
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
            bot_enabled.setdefault(phone, False)
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
<label for="pw">סיסמה (ADMIN_PASSWORD)</label>
<input id="pw" type="password" name="password" required autocomplete="current-password">
{% if err %}<div class="err">{{ err }}</div>{% endif %}
<button type="submit">כניסה</button>
</form>
<p style="margin-top:20px;font-size:12px;color:#5a6378">ניתן להדביק גם את ADMIN_TOKEN בשדה הסיסמה, או לשלוח Authorization: Bearer בקריאות API</p>
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
            hashlib.sha256(pw.encode("utf-8")).digest(),
            hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest(),
        )
        ok_tok = ADMIN_TOKEN and hmac.compare_digest(
            hashlib.sha256(pw.encode("utf-8")).digest(),
            hashlib.sha256(ADMIN_TOKEN.encode("utf-8")).digest(),
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
                "history": history,
                "step": sessions.get(phone, {}).get("step", "active"),
            })
    with state_lock:
        g_on = global_bot_on
    for c in snapshot:
        c["_sort"] = (
            0 if (c["bot_active"] and g_on) else 1,
            -_last_msg_ts_key(c["last_message"]),
            c.get("phone","")
        )
    snapshot.sort(key=lambda c: c["_sort"])
    for c in snapshot:
        del c["_sort"]
    return jsonify(snapshot)


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
    return jsonify({"global_bot_on": v})


@app.route("/api/sync-chats", methods=["POST"])
def api_sync_chats():
    if not GREEN_API_URL or not GREEN_API_TOKEN:
        return jsonify({"ok": False, "error": "Green API לא מוגדר (INSTANCE/TOKEN)"})
    try:
        url = f"{GREEN_API_URL}/getChats/{GREEN_API_TOKEN}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"Green API: {r.status_code} — {r.text[:120]}"})
        chats_data = r.json()
        if not isinstance(chats_data, list):
            return jsonify({"ok": False, "error": "תגובה לא צפויה מ-getChats"})
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
        save_data()
        return jsonify({"phone": phone, "bot_active": now_active})
    if need_greeting:
        greet = f"{get_greeting()} מה נשמע? 😊"
        sent = send_message(phone, greet)
        if sent:
            with state_lock:
                greeting_sent[phone] = True
                sessions.setdefault(phone, {"step": "active", "data": {}})
            add_to_history(phone, "bot", greet)
        else:
            with state_lock:
                bot_enabled[phone] = False
                now_active = False
    save_data()
    return jsonify({"phone": phone, "bot_active": now_active})


@app.route("/api/add-contact", methods=["POST"])
def api_add_contact():
    """הוסף לקוח חדש לפאנל"""
    data = request.get_json(force=True)
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "נדרש מספר טלפון"})
    if phone.startswith("0"):
        phone = "972" + phone[1:]
    if not phone.startswith("972"):
        phone = "972" + phone
    with state_lock:
        chat_history.setdefault(phone, [])
        bot_enabled.setdefault(phone, False)
        sessions.setdefault(phone, {"step": "active", "data": {}})
    save_data()
    return jsonify({"ok": True, "phone": phone})


@app.route("/api/resend-last/<path:phone>", methods=["POST"])
def api_resend_last(phone):
    """שלח שוב את ההודעה האחרונה של הבוט"""
    with state_lock:
        history = list(chat_history.get(phone, []))
    bot_msgs = [h for h in history if h["sender"] == "bot"]
    if not bot_msgs:
        return jsonify({"ok": False, "error": "אין הודעות בוט"})
    last_msg = bot_msgs[-1]["message"]
    if last_msg.startswith("[תזכורת] "):
        last_msg = last_msg[9:]
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
:root{--bg:#0b0d12;--s1:#13161f;--s2:#1a1e2a;--s3:#222736;--border:#252b3b;--accent:#25d366;--text:#dde1ec;--muted:#5a6378;--danger:#e74c3c;}
body{font-family:'Heebo',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{background:var(--s1);border-bottom:1px solid var(--border);padding:0 20px;height:56px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;gap:12px}
.logo{display:flex;align-items:center;gap:9px;font-weight:800;font-size:16px}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,var(--accent),#128c7e);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:16px}
.hdr-mid{display:flex;align-items:center;gap:8px;flex:1;max-width:720px;flex-wrap:wrap}
.search-box{flex:1;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none}
.search-box:focus{border-color:var(--accent)}
.search-box::placeholder{color:var(--muted)}
.btn-global{border:none;border-radius:8px;padding:6px 14px;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.btn-enable-all{border:none;border-radius:8px;padding:6px 12px;font:inherit;font-size:11px;font-weight:700;cursor:pointer;background:rgba(37,211,102,.2);color:var(--accent);border:1px solid var(--accent);white-space:nowrap}
.btn-disable-all{border:none;border-radius:8px;padding:6px 12px;font:inherit;font-size:11px;font-weight:700;cursor:pointer;background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger);white-space:nowrap}
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
.chat-win{flex:1;display:flex;flex-direction:column;background:var(--bg)}
.topbar{padding:10px 18px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;gap:8px}
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
.messages{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:7px}
.messages::-webkit-scrollbar{width:3px}
.messages::-webkit-scrollbar-thumb{background:var(--border)}
.msg{max-width:65%;padding:8px 12px;border-radius:11px;font-size:13px;line-height:1.5;white-space:pre-wrap;animation:fi .15s ease}
@keyframes fi{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.18);align-self:flex-start;border-bottom-left-radius:3px}
.msg-meta{font-size:9px;color:var(--muted);margin-top:2px}
.msg.client .msg-meta{text-align:right}
.msg-icon{display:inline-block;margin-left:3px;font-size:11px}
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
/* modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:24px;width:320px}
.modal h3{margin-bottom:14px;font-size:16px}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-family:inherit;font-size:14px;outline:none;margin-bottom:12px;direction:ltr}
.modal input:focus{border-color:var(--accent)}
.modal-btns{display:flex;gap:8px;justify-content:flex-end}
.btn-cancel{background:var(--s2);color:var(--muted);border:1px solid var(--border);border-radius:7px;padding:7px 14px;font-family:inherit;font-size:13px;cursor:pointer}
.btn-confirm{background:var(--accent);color:#000;border:none;border-radius:7px;padding:7px 14px;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="logo-icon">🔧</div>מרכז שירות</div>
  <div class="hdr-mid">
    <input class="search-box" id="search" placeholder="🔍 חפש מספר או טקסט..." oninput="load()">
    <button class="btn-global on" id="global-btn" onclick="toggleGlobal()" title="הפעל/כבה מענה חדש">🟢 מענה פעיל</button>
    <button class="btn-enable-all" onclick="syncChats()" title="סנכרן שיחות מוואטסאפ">🔄 סנכרן</button>
    <button class="btn-enable-all" onclick="enableAll()" title="הפעל בוט לכל השיחות">⚡ לכולם</button>
    <button class="btn-disable-all" onclick="disableAll()" title="כבה בוט לכל השיחות">⏸ כבה לכולם</button>
  </div>
  <div class="stats">
    <div class="stat">שיחות <b id="s1">0</b></div>
    <div class="stat">קריאות <b id="s2">0</b></div>
  </div>
</header>
<div class="main">
  <div class="calls-panel">
    <div class="cp-head">קריאות שירות</div>
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
    <div class="chat-list" id="list"><div class="no-items">ממתין...</div></div>
  </div>
</div>

<!-- Modal הוספת לקוח -->
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

<script>
let chats=[], calls=[], sel=null, globalOn=true;
const TYPE_ICONS={"image":"📷","audio":"🎤","video":"🎬","document":"📄","sticker":"😀","text":""};
function api(u,o){return fetch(u,Object.assign({},o||{},{credentials:'include'}));}

async function load(){
  const q=document.getElementById('search').value;
  const [cr,sr,gr]=await Promise.all([
    api('/api/chats'+(q?'?q='+encodeURIComponent(q):'')),
    api('/api/service-calls'),
    api('/api/global-status')
  ]);
  chats=await cr.json(); calls=await sr.json(); const gs=await gr.json();
  globalOn=gs.global_bot_on;
  const btn=document.getElementById('global-btn');
  if(globalOn){btn.className='btn-global on';btn.textContent='🟢 פעיל';}
  else{btn.className='btn-global off';btn.textContent='🔴 כבוי';}
  document.getElementById('s1').textContent=chats.length;
  document.getElementById('s2').textContent=calls.length;
  renderList(); renderCalls();
  if(sel){const c=chats.find(c=>c.phone===sel);if(c)renderWin(c);}
}

function renderList(){
  const el=document.getElementById('list');
  if(!chats.length){el.innerHTML='<div class="no-items">ממתין להודעות נכנסות...</div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="ci${c.phone===sel?' active':''}" onclick="pick('${c.phone}')">
      <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
      <div class="ci-info">
        <div class="ci-phone">${fmt(c.phone)}</div>
        <div class="ci-last">${c.last_message?(TYPE_ICONS[c.last_message.type]||'')+' '+esc(c.last_message.message).substring(0,32)+'...':'ממתין...'}</div>
      </div>
      <div onclick="event.stopPropagation()">
        <label class="tgl"><input type="checkbox"${c.bot_active?' checked':''} onchange="tog('${c.phone}')"><span class="tsl"></span></label>
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
      <select class="status-sel" onchange="updateStatus(${c.id},this.value)">
        <option${c.status==='ממתינה לטיפול'?' selected':''}>ממתינה לטיפול</option>
        <option${c.status==='בטיפול'?' selected':''}>בטיפול</option>
        <option${c.status==='הושלמה'?' selected':''}>הושלמה</option>
        <option${c.status==='בוטלה'?' selected':''}>בוטלה</option>
      </select>
    </div>`).join('');
}

function renderWin(c){
  const h=c.history||[];
  const isActive=c.bot_active&&globalOn;
  document.getElementById('win').innerHTML=`
    <div class="topbar">
      <div class="tb-left">
        <div style="font-size:19px">👤</div>
        <div><div class="tb-phone">${fmt(c.phone)}</div><div class="tb-sub">${h.length} הודעות</div></div>
      </div>
      <div class="tb-right">
        <span class="badge${isActive?' on':''}">${isActive?'🤖 פעיל':'⏸ כבוי'}</span>
        <button class="btn-sm btn-resend" onclick="resendLast('${c.phone}')" title="שלח שוב הודעה אחרונה">🔄 שלח שוב</button>
        ${c.bot_active
          ?`<button class="btn-sm btn-deact" onclick="tog('${c.phone}')">⏸ כבה</button>`
          :`<button class="btn-sm btn-act" onclick="tog('${c.phone}')">▶ ${c.greeting_sent?'הפעל':'שלח פתיחה'}</button>`}
      </div>
    </div>
    <div class="messages" id="msgs">
      ${h.length?h.map(m=>`
        <div class="msg ${m.sender}">
          ${m.type&&m.type!=='text'?'<span class="msg-icon">'+TYPE_ICONS[m.type]+'</span>':''}${esc(m.message)}
          <div class="msg-meta">${m.sender==='bot'?'🤖 ':''}${m.time}</div>
        </div>`).join('')
        :'<div style="text-align:center;color:var(--muted);font-size:12px;margin-top:30px">אין הודעות</div>'}
    </div>`;
  const msgs=document.getElementById('msgs');
  if(msgs)msgs.scrollTop=msgs.scrollHeight;
}

function pick(phone){sel=phone;const c=chats.find(c=>c.phone===phone);if(c)renderWin(c);renderList();}
async function tog(phone){await api('/api/toggle/'+phone,{method:'POST'});await load();}
async function toggleGlobal(){await api('/api/global-toggle',{method:'POST'});await load();}
async function syncChats(){
  const r=await api('/api/sync-chats',{method:'POST'});
  const d=await r.json();
  if(d.ok){await load();alert('✅ סונכרנו '+d.synced+' שיחות');}
  else alert('שגיאה: '+(d.error||''));
}
async function enableAll(){
  await api('/api/enable-all',{method:'POST'});
  await load();
}
async function disableAll(){
  await api('/api/disable-all',{method:'POST'});
  await load();
}
async function resendLast(phone){
  const r=await api('/api/resend-last/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('אין הודעה לשליחה חוזרת');
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
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
load();setInterval(load,4000);
</script>
</body>
</html>"""


MOBILE = r"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>בוט שירות</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;600;700;900&display=swap');
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{--bg:#0b0d12;--s1:#13161f;--s2:#1a1e2a;--s3:#222736;--border:#252b3b;--accent:#25d366;--text:#dde1ec;--muted:#5a6378;--danger:#e74c3c;}
body{font-family:'Heebo',sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
.hdr{background:var(--s1);border-bottom:1px solid var(--border);padding:10px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.hdr-icon{width:30px;height:30px;background:linear-gradient(135deg,#25d366,#128c7e);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.hdr-title{font-weight:800;font-size:15px;flex:1}
.btn-global{border:none;border-radius:18px;padding:5px 12px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.btn-mbar{border:none;border-radius:16px;padding:5px 10px;font:inherit;font-size:10px;font-weight:700;cursor:pointer;background:rgba(37,211,102,.2);color:var(--accent);border:1px solid var(--accent);white-space:nowrap}
.search-bar{padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0}
.search-input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:14px;outline:none}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:var(--muted)}
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--border);flex-shrink:0}
.tab{flex:1;padding:11px;text-align:center;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.page{display:none;flex:1;overflow-y:auto;flex-direction:column}
.page.active{display:flex}
.add-btn-wrap{padding:10px 12px 4px;flex-shrink:0}
.btn-add-full{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:12px;padding:11px;font-family:inherit;font-size:14px;font-weight:600;color:var(--accent);cursor:pointer;text-align:center}
.cards{padding:8px 12px 20px;display:flex;flex-direction:column;gap:9px}
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
.card-btn:hover{background:var(--s2)}
.call-card{background:var(--s1);border:1px solid var(--border);border-radius:13px;padding:13px;margin:0 12px 10px;font-size:12px}
.call-id{font-size:10px;color:var(--muted);margin-bottom:4px}
.call-name{font-weight:700;font-size:14px;margin-bottom:3px}
.call-type{color:var(--accent);font-size:11px;margin-bottom:7px}
.call-row{color:var(--muted);margin-bottom:3px}
.call-row span{color:var(--text)}
.status-sel{margin-top:9px;width:100%;background:var(--s3);border:1px solid var(--border);border-radius:9px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none;cursor:pointer}
.chat-view{display:none;position:fixed;inset:0;background:var(--bg);flex-direction:column;z-index:100}
.chat-view.active{display:flex}
.chat-hdr{padding:11px 14px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px;flex-shrink:0}
.back-btn{background:none;border:none;color:var(--accent);font-size:22px;cursor:pointer;line-height:1;padding:0 2px}
.chat-hdr-info{flex:1}
.chat-hdr-phone{font-weight:700;font-size:14px}
.chat-hdr-sub{font-size:10px;color:var(--muted)}
.chat-hdr-btns{display:flex;gap:6px;align-items:center}
.btn-xs{border:none;border-radius:7px;padding:5px 9px;font-family:inherit;font-size:11px;font-weight:700;cursor:pointer}
.btn-act{background:var(--accent);color:#000}
.btn-deact{background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.btn-resend{background:var(--s3);color:var(--text);border:1px solid var(--border)}
.msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:6px}
.msg{max-width:80%;padding:8px 11px;border-radius:11px;font-size:13px;line-height:1.5;white-space:pre-wrap}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.2);align-self:flex-start;border-bottom-left-radius:3px}
.msg-time{font-size:9px;color:var(--muted);margin-top:2px}
.msg.client .msg-time{text-align:right}
.empty{padding:40px 16px;text-align:center;color:var(--muted)}
.empty div:first-child{font-size:40px;opacity:.3;margin-bottom:8px}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:flex-end}
.modal-bg.open{display:flex}
.modal{background:var(--s1);border-top:1px solid var(--border);border-radius:20px 20px 0 0;padding:20px;width:100%}
.modal h3{margin-bottom:12px;font-size:16px}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:12px;color:var(--text);font-family:inherit;font-size:15px;outline:none;margin-bottom:12px;direction:ltr}
.modal-btns{display:flex;gap:8px}
.btn-cancel{flex:1;background:var(--s2);color:var(--muted);border:1px solid var(--border);border-radius:10px;padding:11px;font-family:inherit;font-size:14px;cursor:pointer}
.btn-confirm{flex:2;background:var(--accent);color:#000;border:none;border-radius:10px;padding:11px;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-icon">🔧</div>
  <div class="hdr-title">בוט שירות</div>
  <button class="btn-global on" id="g-btn" onclick="toggleGlobal()">🟢 פעיל</button>
  <button class="btn-mbar" onclick="syncChats()">🔄</button>
  <button class="btn-mbar" onclick="enableAll()">⚡</button>
</div>
<div class="search-bar">
  <input class="search-input" id="search" placeholder="🔍 חפש מספר או טקסט..." oninput="load()">
</div>
<div class="tabs">
  <div class="tab active" onclick="showTab('clients')">👥 לקוחות</div>
  <div class="tab" onclick="showTab('calls')">🔧 קריאות</div>
</div>
<div class="page active" id="page-clients">
  <div class="add-btn-wrap">
    <button class="btn-add-full" onclick="openModal()">+ פנה ללקוח חדש</button>
  </div>
  <div class="cards" id="cards"></div>
</div>
<div class="page" id="page-calls">
  <div id="calls-list" style="padding-top:8px"></div>
</div>
<div class="chat-view" id="chat-view">
  <div class="chat-hdr">
    <button class="back-btn" onclick="closeChat()">‹</button>
    <div class="chat-hdr-info">
      <div class="chat-hdr-phone" id="cv-phone"></div>
      <div class="chat-hdr-sub" id="cv-sub"></div>
    </div>
    <div class="chat-hdr-btns">
      <button class="btn-xs btn-resend" onclick="resendLast()">🔄</button>
      <label class="tgl"><input type="checkbox" id="cv-tgl" onchange="cvToggle()"><span class="tsl"></span></label>
    </div>
  </div>
  <div class="msgs" id="cv-msgs"></div>
</div>
<div class="modal-bg" id="modal">
  <div class="modal">
    <h3>📞 פנה ללקוח חדש</h3>
    <input id="m-phone" placeholder="05XXXXXXXX" type="tel">
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeModal()">ביטול</button>
      <button class="btn-confirm" onclick="addContact()">הוסף לפאנל</button>
    </div>
  </div>
</div>
<script>
let chats=[], calls=[], cvPhone=null, globalOn=true;
const TYPE_ICONS={"image":"📷","audio":"🎤","video":"🎬","document":"📄","sticker":"😀","text":""};
function api(u,o){return fetch(u,Object.assign({},o||{},{credentials:'include'}));}

async function load(){
  const q=document.getElementById('search').value;
  const [cr,sr,gr]=await Promise.all([
    api('/api/chats'+(q?'?q='+encodeURIComponent(q):'')),
    api('/api/service-calls'),
    api('/api/global-status')
  ]);
  chats=await cr.json(); calls=await sr.json(); const gs=await gr.json();
  globalOn=gs.global_bot_on;
  const btn=document.getElementById('g-btn');
  if(globalOn){btn.className='btn-global on';btn.textContent='🟢 פעיל';}
  else{btn.className='btn-global off';btn.textContent='🔴 כבוי';}
  renderCards(); renderCalls();
  if(cvPhone){const c=chats.find(c=>c.phone===cvPhone);if(c)updateCV(c);}
}

function showTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',['clients','calls'][i]===t));
  document.querySelectorAll('.page').forEach(el=>el.classList.remove('active'));
  document.getElementById('page-'+t).classList.add('active');
}

function renderCards(){
  const el=document.getElementById('cards');
  if(!chats.length){el.innerHTML='<div class="empty"><div>💬</div><div>ממתין להודעות</div></div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="card${c.bot_active?' on':''}">
      <div class="card-top">
        <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
        <div class="ci">
          <div class="ci-phone">${fmt(c.phone)}</div>
          <div class="ci-last">${c.last_message?(TYPE_ICONS[c.last_message.type]||'')+' '+esc(c.last_message.message).substring(0,38):'ממתין...'}</div>
        </div>
        <label class="tgl" onclick="event.stopPropagation()">
          <input type="checkbox"${c.bot_active?' checked':''} onchange="tog('${c.phone}')">
          <span class="tsl"></span>
        </label>
      </div>
      <div class="card-btns">
        <button class="card-btn" onclick="openChat('${c.phone}')">💬 שיחה (${(c.history||[]).length})</button>
        <button class="card-btn" onclick="resendLastFor('${c.phone}')">🔄 שלח שוב</button>
      </div>
    </div>`).join('');
}

function renderCalls(){
  const el=document.getElementById('calls-list');
  if(!calls.length){el.innerHTML='<div class="empty"><div>🔧</div><div>אין קריאות עדיין</div></div>';return;}
  el.innerHTML=[...calls].reverse().map(c=>`
    <div class="call-card">
      <div class="call-id">${c.opened_at}</div>
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
  const c=chats.find(c=>c.phone===phone);
  if(c)updateCV(c);
  document.getElementById('chat-view').classList.add('active');
}

function updateCV(c){
  document.getElementById('cv-phone').textContent=fmt(c.phone);
  document.getElementById('cv-sub').textContent=(c.history||[]).length+' הודעות';
  document.getElementById('cv-tgl').checked=c.bot_active;
  const msgs=document.getElementById('cv-msgs');
  const h=c.history||[];
  msgs.innerHTML=h.length?h.map(m=>`
    <div class="msg ${m.sender}">
      ${m.type&&m.type!=='text'?(TYPE_ICONS[m.type]||'')+' ':''}${esc(m.message)}
      <div class="msg-time">${m.sender==='bot'?'🤖 ':''}${m.time}</div>
    </div>`).join('')
    :'<div style="text-align:center;color:var(--muted);font-size:13px;margin-top:30px">אין הודעות</div>';
  msgs.scrollTop=msgs.scrollHeight;
}

function closeChat(){document.getElementById('chat-view').classList.remove('active');cvPhone=null;}
async function cvToggle(){if(cvPhone){await api('/api/toggle/'+cvPhone,{method:'POST'});await load();}}
async function resendLast(){if(cvPhone)await resendLastFor(cvPhone);}
async function resendLastFor(phone){
  const r=await api('/api/resend-last/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('אין הודעה לשליחה חוזרת');
  else{await load();if(cvPhone===phone){const c=chats.find(c=>c.phone===phone);if(c)updateCV(c);}}
}
async function tog(phone){await api('/api/toggle/'+phone,{method:'POST'});await load();}
async function toggleGlobal(){await api('/api/global-toggle',{method:'POST'});await load();}
async function syncChats(){
  const r=await api('/api/sync-chats',{method:'POST'});
  const d=await r.json();
  if(d.ok){await load();alert('✅ סונכרנו '+d.synced+' שיחות');}
  else alert('שגיאה: '+(d.error||''));
}
async function enableAll(){
  await api('/api/enable-all',{method:'POST'});
  await load();
}
async function updateStatus(id,status){
  await api('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function openModal(){document.getElementById('modal').classList.add('open');setTimeout(()=>document.getElementById('m-phone').focus(),100);}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('m-phone').value='';}
async function addContact(){
  const phone=document.getElementById('m-phone').value.trim();
  if(!phone){alert('הזן מספר');return;}
  const r=await api('/api/add-contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
  const d=await r.json();
  if(d.ok){closeModal();await load();openChat(d.phone);}
  else alert(d.error||'שגיאה');
}
function fmt(p){return String(p).replace('@c.us','').replace(/^972/,'0');}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
load();setInterval(load,4000);
</script>
</body>
</html>"""


def fetch_google_contacts():
    """משיכת אנשי קשר מגוגל"""
    global google_contacts
    token = google_tokens.get("access_token")
    if not token:
        return
    try:
        r = requests.get(
            "https://people.googleapis.com/v1/people/me/connections",
            params={"personFields": "names,phoneNumbers", "pageSize": 1000},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if r.status_code == 401:
            refresh_google_token()
            return
        data = r.json()
        contacts = []
        for p in data.get("connections", []):
            names = p.get("names", [{}])
            name = names[0].get("displayName", "") if names else ""
            phones = p.get("phoneNumbers", [])
            for ph in phones:
                num = ph.get("value", "").replace("-","").replace(" ","").replace("+972","0").replace("972","0")
                if num:
                    contacts.append({"name": name, "phone": num})
        google_contacts = contacts
        print(f"[Google] loaded {len(contacts)} contacts", flush=True)
    except Exception as e:
        print(f"[Google] fetch error: {e}", flush=True)


def refresh_google_token():
    """רענון טוקן גוגל"""
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": google_tokens.get("refresh_token"),
            "grant_type": "refresh_token"
        })
        d = r.json()
        if "access_token" in d:
            google_tokens["access_token"] = d["access_token"]
            fetch_google_contacts()
    except Exception as e:
        print(f"[Google] refresh error: {e}", flush=True)


def search_google_contacts(query):
    """חיפוש איש קשר"""
    if not google_contacts:
        return None
    query_lower = query.lower()
    results = [c for c in google_contacts if query_lower in c.get("name","").lower() or query_lower in c.get("phone","")]
    return results[:5] if results else None


@app.route("/google-auth")
def google_auth():
    if not GOOGLE_CLIENT_ID:
        return "GOOGLE_CLIENT_ID לא מוגדר ב-Render Environment", 400
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/contacts.readonly",
        "access_type": "offline",
        "prompt": "consent"
    })
    return f'<meta http-equiv="refresh" content="0;url=https://accounts.google.com/o/oauth2/v2/auth?{params}">'


@app.route("/google-callback")
def google_callback():
    code = request.args.get("code")
    if not code:
        return "שגיאה: לא התקבל קוד", 400
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"
        })
        tokens = r.json()
        google_tokens["access_token"] = tokens.get("access_token")
        google_tokens["refresh_token"] = tokens.get("refresh_token")
        fetch_google_contacts()
        return f"<h2>✅ גוגל חובר! נטענו {len(google_contacts)} אנשי קשר.</h2>"
    except Exception as e:
        return f"שגיאה: {e}", 500


@app.route("/api/google-status")
def api_google_status():
    return jsonify({
        "connected": bool(google_tokens.get("access_token")),
        "contacts": len(google_contacts)
    })


@app.route("/ping")
def ping():
    return "ok"


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD)

@app.route("/mobile")
def mobile():
    return render_template_string(MOBILE)

def polling_loop():
    """משאל את Green API כל 3 שניות להודעות חדשות"""
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
                    print(f"[Polling] type={body.get('typeWebhook','')} sender={body.get('senderData',{})}", flush=True)
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
    _polling_thread = threading.Thread(target=polling_loop, daemon=True)
    _polling_thread.start()

if ENABLE_KEEP_ALIVE and KEEP_ALIVE_URL:
    threading.Thread(target=_keep_alive_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=FLASK_DEBUG, host="0.0.0.0", port=FLASK_PORT)
