from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timezone
import requests
import re
import json
import threading
import time
import os

app = Flask(__name__)

# ─── הגדרות ───────────────────────────────────────────────────
GREEN_API_INSTANCE  = "7107555828"
GREEN_API_TOKEN     = "3bd4a6dac146413bb8fa7deff8cfc91cc61f10a392034aec97"
GREEN_API_URL       = f"https://7107.api.greenapi.com/waInstance{GREEN_API_INSTANCE}"
NOTIFY_PHONE        = "972527066110"
BUSINESS_NAME       = "שירות לקוחות"
GREETING_MSG        = "היי! איך אפשר לעזור? 😊"
ANTHROPIC_KEY       = os.environ.get("ANTHROPIC_KEY", "")
CLAUDE_API_URL      = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL        = "claude-sonnet-4-20250514"

# ─── נתונים ───────────────────────────────────────────────────
sessions      = {}
service_calls = []
bot_enabled   = {}
chat_history  = {}
greeting_sent = {}
global_bot_on = True
last_bot_msg_time = {}   # phone -> timestamp of last bot message
reminder_timers   = {}   # phone -> timer thread

SYSTEM_PROMPT = """אתה נציג שירות של חברת בריכות שחייה. אתה מנהל שיחת וואטסאפ טבעית עם לקוחות.

הסגנון שלך:
- עברית יומיומית, חמה וטבעית — כמו בן אדם אמיתי
- קצר וענייני, לא רובוטי ולא פורמלי מדי
- הגב בהתאם להקשר — אם הלקוח כותב "שלום" תגיב בחמימות, אם הוא מתאר תקלה תתמקד בה מיד
- אל תשאל את כל השאלות בבת אחת — שאל שאלה אחת בכל פעם בצורה טבעית

הפרטים שצריך לאסוף (בהדרגה, בתוך השיחה):
1. שם
2. כתובת הבריכה (רחוב, מספר, עיר)
3. סוג הפנייה: תקלה/תיקון, תחזוקה, בריכה חדשה, שיפוץ, או משהו אחר
4. תיאור הבעיה או הבקשה
5. טלפון ליצירת קשר

כללים:
- אם הלקוח מתאר בעיה בבריכה — הגב עם הבנה ואז שאל את מה שחסר
- אם שלח תמונה — הגב על זה טבעית והמשך לאסוף פרטים
- אם לא רוצה שירות — סגור בנימוס
- אחרי שיש לך את כל הפרטים — הצג סיכום קצר ובקש אישור
- אחרי אישור — החזר JSON בדיוק כך (ללא טקסט נוסף):
  {"action":"open_call","name":"...","address":"...","call_type":"...","description":"...","contact_phone":"..."}
- אם ביטל — החזר: {"action":"cancelled"}
- אחרת — החזר: {"action":"continue","message":"הודעה ללקוח"}
- אל תציין מספר קריאה בשיחה"""


def send_message(phone, text):
    try:
        url = f"{GREEN_API_URL}/sendMessage/{GREEN_API_TOKEN}"
        chat_id = phone if "@c.us" in phone else f"{phone}@c.us"
        r = requests.post(url, json={"chatId": chat_id, "message": text}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[GreenAPI] error: {e}")
        return False


def add_to_history(phone, sender, message, msg_type="text"):
    chat_history.setdefault(phone, []).append({
        "sender": sender, "message": message,
        "time": datetime.now().strftime("%H:%M"),
        "type": msg_type
    })


def get_session(phone):
    if phone not in sessions:
        sessions[phone] = {"step": "active", "data": {}}
    return sessions[phone]


def reset_session(phone):
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


def ask_claude(history, user_msg, msg_type="text"):
    try:
        messages = []
        for h in history[-14:]:
            role = "user" if h["sender"] == "client" else "assistant"
            # בנה תוכן בהתאם לסוג ההודעה
            if h.get("type") in ["image","audio","document","video","sticker"] and h["sender"] == "client":
                content = f"[הלקוח שלח {h.get('type','קובץ')}] {h['message']}"
            else:
                content = h["message"]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{content}"
            else:
                messages.append({"role": role, "content": content})

        # הוסף הודעה נוכחית
        if msg_type in ["image","audio","document","video","sticker"]:
            current_msg = f"[הלקוח שלח {msg_type}] {user_msg}"
        else:
            current_msg = user_msg

        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += f"\n{current_msg}"
        else:
            messages.append({"role": "user", "content": current_msg})

        resp = requests.post(
            CLAUDE_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 600,
                "system": SYSTEM_PROMPT,
                "messages": messages
            },
            timeout=20
        )
        text = resp.json()["content"][0]["text"].strip()
        try:
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start != -1 and end > start:
                return json.loads(text[start:end])
        except:
            pass
        return {"action": "continue", "message": text}
    except Exception as e:
        print(f"[Claude] error: {e}")
        return {"action": "continue", "message": "מצטערים, אירעה שגיאה זמנית. נסה שוב בעוד רגע."}


def cancel_reminder(phone):
    if phone in reminder_timers:
        reminder_timers[phone].cancel()
        del reminder_timers[phone]


def schedule_reminder(phone, last_msg):
    """שולח תזכורת אחרי 30 שניות אם הלקוח לא ענה"""
    cancel_reminder(phone)
    def remind():
        if bot_enabled.get(phone, False) and global_bot_on:
            send_message(phone, last_msg)
            add_to_history(phone, "bot", f"[תזכורת] {last_msg}")
    t = threading.Timer(30.0, remind)
    t.daemon = True
    t.start()
    reminder_timers[phone] = t


def handle_message(phone, body, msg_type="text"):
    cancel_reminder(phone)  # ביטול תזכורת קודמת
    history = chat_history.get(phone, [])

    result = ask_claude(history, body, msg_type)
    action = result.get("action", "continue")

    if action == "open_call":
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
        send_message(NOTIFY_PHONE, build_notify_message(phone, result))
        reset_session(phone)
        reply = (
            f"✅ *הקריאה נפתחה בהצלחה!*\n\n"
            f"נציג יצור איתך קשר בהקדם.\n"
            f"תודה שפנית ל{BUSINESS_NAME}! 🙏\n\n"
            f"לקריאה נוספת — כתוב לי בכל עת 😊"
        )
        return reply

    if action == "cancelled":
        reset_session(phone)
        return "בסדר גמור! אם תצטרך עזרה בעתיד — אנחנו כאן. 🙏"

    reply = result.get("message", "לא הבנתי, נסה שוב.")
    # קבע תזכורת
    schedule_reminder(phone, reply)
    return reply


# ─── Webhook ──────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return "ok"

        webhook_type = data.get("typeWebhook", "")
        msg_data = data.get("messageData", {})
        sender   = data.get("senderData", {})
        print(f"[Webhook] type={webhook_type} sender={sender} data={data}", flush=True)

        def get_phone():
            return sender.get("chatId", "").replace("@c.us", "")

        def parse_body():
            msg_type_raw = msg_data.get("typeMessage", "textMessage")
            type_map = {
                "textMessage":     ("text",     lambda d: d.get("textMessageData",{}).get("textMessage","")),
                "imageMessage":    ("image",    lambda d: "[שלח תמונה]"),
                "audioMessage":    ("audio",    lambda d: "[שלח הקלטה קולית]"),
                "videoMessage":    ("video",    lambda d: "[שלח וידאו]"),
                "documentMessage": ("document", lambda d: "[שלח מסמך]"),
                "stickerMessage":  ("sticker",  lambda d: "[שלח סטיקר]"),
                "locationMessage": ("text",     lambda d: "[שיתף מיקום]"),
                "contactMessage":  ("text",     lambda d: "[שיתף איש קשר]"),
            }
            msg_type, extractor = type_map.get(msg_type_raw, ("text", lambda d: ""))
            return msg_type, extractor(msg_data) or ""

        # הודעה נכנסת מלקוח
        if webhook_type == "incomingMessageReceived":
            phone = get_phone()
            if not phone or is_group(phone + "@c.us"):
                return "ok"
            msg_type, body_text = parse_body()
            if not body_text:
                return "ok"
            # תמיד רשום בפורטל, toggle כבוי כברירת מחדל
            if phone not in bot_enabled:
                bot_enabled[phone] = False
            add_to_history(phone, "client", body_text, msg_type)
            sessions.setdefault(phone, {"step": "active", "data": {}})
            # ענה רק אם הבוט מופעל ידנית
            if bot_enabled.get(phone, False) and global_bot_on:
                reply = handle_message(phone, body_text, msg_type)
                add_to_history(phone, "bot", reply)
                send_message(phone, reply)

        # הודעה יוצאת (שלחת מהוואטסאפ או מהפורטל)
        elif webhook_type == "outgoingMessageReceived":
            phone = get_phone()
            if not phone or is_group(phone + "@c.us"):
                return "ok"
            _, body_text = parse_body()
            if not body_text:
                return "ok"
            if phone not in bot_enabled:
                bot_enabled[phone] = False
            add_to_history(phone, "bot", body_text, "text")
            sessions.setdefault(phone, {"step": "active", "data": {}})

    except Exception as e:
        print(f"[Webhook] error: {e}")
    return "ok"


# ─── API ──────────────────────────────────────────────────────
@app.route("/api/chats")
def api_chats():
    search = request.args.get("q", "").strip().lower()
    all_phones = set(list(chat_history.keys()) + list(bot_enabled.keys()))
    result = []
    for phone in all_phones:
        if is_group(phone + "@c.us"):
            continue
        history = chat_history.get(phone, [])
        last = history[-1] if history else None

        # חיפוש
        if search:
            phone_match = search in phone.replace("972","0",1)
            text_match  = any(search in h["message"].lower() for h in history)
            if not phone_match and not text_match:
                continue

        result.append({
            "phone":         phone,
            "bot_active":    bot_enabled.get(phone, False),
            "greeting_sent": greeting_sent.get(phone, False),
            "last_message":  last,
            "history":       history,
            "step":          sessions.get(phone, {}).get("step", "active")
        })

    # מיין: בוט פעיל קודם, אחר כך לפי זמן
    def sort_key(c):
        active = 0 if c["bot_active"] else 1
        t = c["last_message"]["time"] if c["last_message"] else "00:00"
        return (active, t)

    result.sort(key=sort_key, reverse=False)
    result = list(reversed(result))
    return jsonify(result)


@app.route("/api/global-toggle", methods=["POST"])
def api_global_toggle():
    global global_bot_on
    global_bot_on = not global_bot_on
    return jsonify({"global_bot_on": global_bot_on})


@app.route("/api/global-status")
def api_global_status():
    return jsonify({"global_bot_on": global_bot_on})


@app.route("/api/toggle/<path:phone>", methods=["POST"])
def api_toggle(phone):
    was_active = bot_enabled.get(phone, False)
    bot_enabled[phone] = not was_active
    now_active = bot_enabled[phone]
    if now_active and not greeting_sent.get(phone, False):
        sent = send_message(phone, GREETING_MSG)
        if sent:
            greeting_sent[phone] = True
            add_to_history(phone, "bot", GREETING_MSG)
            sessions.setdefault(phone, {"step": "active", "data": {}})
    if not now_active:
        cancel_reminder(phone)
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
    # הוסף לפאנל בלי להפעיל בוט
    if phone not in chat_history:
        chat_history[phone] = []
    bot_enabled.setdefault(phone, False)
    sessions.setdefault(phone, {"step": "active", "data": {}})
    return jsonify({"ok": True, "phone": phone})


@app.route("/api/resend-last/<path:phone>", methods=["POST"])
def api_resend_last(phone):
    """שלח שוב את ההודעה האחרונה של הבוט"""
    history = chat_history.get(phone, [])
    bot_msgs = [h for h in history if h["sender"] == "bot"]
    if not bot_msgs:
        return jsonify({"ok": False, "error": "אין הודעות בוט"})
    last_msg = bot_msgs[-1]["message"]
    # הסר תזכורת prefix
    if last_msg.startswith("[תזכורת] "):
        last_msg = last_msg[9:]
    sent = send_message(phone, last_msg)
    if sent:
        add_to_history(phone, "bot", f"[נשלח שוב] {last_msg}")
    return jsonify({"ok": sent})


@app.route("/api/service-calls")
def api_service_calls():
    return jsonify(service_calls)


@app.route("/api/service-calls/<int:call_id>/status", methods=["POST"])
def api_update_status(call_id):
    data = request.get_json(force=True)
    for call in service_calls:
        if call["id"] == call_id:
            call["status"] = data.get("status", "")
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
.hdr-mid{display:flex;align-items:center;gap:8px;flex:1;max-width:340px}
.search-box{flex:1;background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none}
.search-box:focus{border-color:var(--accent)}
.search-box::placeholder{color:var(--muted)}
.btn-global{border:none;border-radius:8px;padding:6px 14px;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
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
    <button class="btn-global on" id="global-btn" onclick="toggleGlobal()">🟢 פעיל</button>
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

async function load(){
  const q=document.getElementById('search').value;
  const [cr,sr,gr]=await Promise.all([
    fetch('/api/chats'+(q?'?q='+encodeURIComponent(q):'')),
    fetch('/api/service-calls'),
    fetch('/api/global-status')
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
async function tog(phone){await fetch('/api/toggle/'+phone,{method:'POST'});await load();}
async function toggleGlobal(){await fetch('/api/global-toggle',{method:'POST'});await load();}
async function resendLast(phone){
  const r=await fetch('/api/resend-last/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('אין הודעה לשליחה חוזרת');
  else await load();
}
async function updateStatus(id,status){
  await fetch('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function openAddContact(){document.getElementById('modal').classList.add('open');document.getElementById('contact-phone').focus();}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('contact-phone').value='';}
async function addContact(){
  const phone=document.getElementById('contact-phone').value.trim();
  if(!phone){alert('הזן מספר');return;}
  const r=await fetch('/api/add-contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
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

async function load(){
  const q=document.getElementById('search').value;
  const [cr,sr,gr]=await Promise.all([
    fetch('/api/chats'+(q?'?q='+encodeURIComponent(q):'')),
    fetch('/api/service-calls'),
    fetch('/api/global-status')
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
async function cvToggle(){if(cvPhone){await fetch('/api/toggle/'+cvPhone,{method:'POST'});await load();}}
async function resendLast(){if(cvPhone)await resendLastFor(cvPhone);}
async function resendLastFor(phone){
  const r=await fetch('/api/resend-last/'+phone,{method:'POST'});
  const d=await r.json();
  if(!d.ok)alert('אין הודעה לשליחה חוזרת');
  else{await load();if(cvPhone===phone){const c=chats.find(c=>c.phone===phone);if(c)updateCV(c);}}
}
async function tog(phone){await fetch('/api/toggle/'+phone,{method:'POST'});await load();}
async function toggleGlobal(){await fetch('/api/global-toggle',{method:'POST'});await load();}
async function updateStatus(id,status){
  await fetch('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function openModal(){document.getElementById('modal').classList.add('open');setTimeout(()=>document.getElementById('m-phone').focus(),100);}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('m-phone').value='';}
async function addContact(){
  const phone=document.getElementById('m-phone').value.trim();
  if(!phone){alert('הזן מספר');return;}
  const r=await fetch('/api/add-contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
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


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD)

@app.route("/mobile")
def mobile():
    return render_template_string(MOBILE)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
