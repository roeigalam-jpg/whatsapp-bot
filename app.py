from flask import Flask, request, jsonify, render_template_string
from datetime import datetime
import requests
import re
import json

app = Flask(__name__)

# ─── הגדרות Green API ─────────────────────────────────────────
GREEN_API_INSTANCE  = "7107555828"
GREEN_API_TOKEN     = "3bd4a6dac146413bb8fa7deff8cfc91cc61f10a392034aec97"
GREEN_API_URL       = f"https://7107.api.greenapi.com/waInstance{GREEN_API_INSTANCE}"

NOTIFY_PHONE        = "972527066110"
BUSINESS_NAME       = "שירות לקוחות"
GREETING_MSG        = "היי! מעוניין לפתוח קריאת שירות/התקנה? 😊"

# ─── נתונים בזיכרון ───────────────────────────────────────────
sessions      = {}
service_calls = []
bot_enabled   = {}
chat_history  = {}
greeting_sent = {}
global_bot_on = True   # מתג גלובלי

# ─── זיהוי כן/לא רחב ──────────────────────────────────────────
YES_WORDS = {
    "כן","yes","כ","מעוניין","בטח","אוקי","ok","בסדר","טוב","יאללה",
    "איך","ברור","בהחלט","כמובן","נכון","יש","אפשר","רוצה","yep","yup",
    "sure","בואו","הכן","ודאי","בוא","רוצה שירות","רוצה קריאה","פתח",
    "פתחו","תפתח","אני רוצה","אני מעוניין","כן בבקשה","בבקשה"
}
NO_WORDS = {
    "לא","no","לא מעוניין","לא רוצה","nope","nah","לא צריך","לא תודה",
    "תודה לא","ביטול","בטל","לא עכשיו","אחר כך","לא היום","לא רלוונטי"
}

def is_yes(msg):
    m = msg.strip().lower()
    if m in YES_WORDS:
        return True
    yes_phrases = ["רוצה שירות","רוצה קריאה","פתח קריאה","צריך עזרה","יש תקלה","יש בעיה"]
    return any(p in m for p in yes_phrases)

def is_no(msg):
    m = msg.strip().lower()
    if m in NO_WORDS:
        return True
    no_phrases = ["לא רוצה","לא מעוניין","לא צריך","לא תודה","תודה לא"]
    return any(p in m for p in no_phrases)

def send_message(phone, text):
    try:
        url = f"{GREEN_API_URL}/sendMessage/{GREEN_API_TOKEN}"
        chat_id = phone if "@c.us" in phone else f"{phone}@c.us"
        r = requests.post(url, json={"chatId": chat_id, "message": text}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[GreenAPI] error: {e}")
        return False

def add_to_history(phone, sender, message):
    chat_history.setdefault(phone, []).append({
        "sender": sender, "message": message,
        "time": datetime.now().strftime("%H:%M")
    })

def get_session(phone):
    if phone not in sessions:
        sessions[phone] = {"step": "wait_greeting", "data": {}}
    return sessions[phone]

def reset_session(phone):
    sessions[phone] = {"step": "wait_greeting", "data": {}}

def is_group(phone):
    return "@g.us" in phone or "g.us" in phone

def build_notify_message(call_id, phone, data):
    client_num = phone.replace("@c.us","").replace("972","0",1)
    return "\n".join([
        f"🔔 *קריאה חדשה #{call_id}*",
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
        f"⚡ נא לפתוח קריאה במערכת ולאשר ללקוח."
    ])

def handle_message(phone, body):
    session = get_session(phone)
    step = session["step"]
    msg = body.strip()

    if step == "wait_greeting":
        if is_yes(msg):
            session["step"] = "ask_name"
            return "מעולה! 😊 נפתח עכשיו קריאה.\n\nשאלה 1/5 ➤ *מה שמך המלא?*"
        if is_no(msg):
            reset_session(phone)
            return "בסדר גמור! אם תצטרך עזרה בעתיד — אנחנו כאן. 🙏"
        # כל הודעה אחרת — הצע שירות
        return "היי! מעוניין לפתוח קריאת שירות/התקנה? 😊\n\nשלח *כן* כדי להתחיל, או *לא* אם אין צורך."

    if step == "ask_name":
        if is_no(msg):
            reset_session(phone)
            return "בסדר! אם תצטרך עזרה בעתיד — אנחנו כאן. 🙏"
        if len(msg) < 2:
            return "נא להזין שם תקין."
        session["data"]["name"] = msg
        session["step"] = "ask_address"
        return f"תודה {msg}! 📝\n\nשאלה 2/5 ➤ *מה הכתובת לביצוע השירות?*\n(רחוב + מספר + עיר)"

    if step == "ask_address":
        if is_no(msg):
            reset_session(phone)
            return "הקריאה בוטלה. שלח *כן* אם תרצה להתחיל מחדש."
        if len(msg) < 5:
            return "נא להזין כתובת מלאה (רחוב, מספר, עיר)."
        session["data"]["address"] = msg
        session["step"] = "ask_call_type"
        return "שאלה 3/5 ➤ *מה סוג הפנייה?*\n\n1️⃣ קריאת שירות / תקלה\n2️⃣ התקנה חדשה\n3️⃣ אחר\n\nשלח 1, 2 או 3:"

    if step == "ask_call_type":
        if is_no(msg):
            reset_session(phone)
            return "הקריאה בוטלה. שלח *כן* אם תרצה להתחיל מחדש."
        types = {"1": "קריאת שירות / תקלה", "2": "התקנה חדשה", "3": "אחר",
                 "שירות": "קריאת שירות / תקלה", "תקלה": "קריאת שירות / תקלה",
                 "התקנה": "התקנה חדשה", "אחר": "אחר"}
        call_type = types.get(msg) or types.get(msg.strip("️"))
        if not call_type:
            return "שלח 1 לשירות/תקלה, 2 להתקנה, או 3 לאחר."
        session["data"]["call_type"] = call_type
        session["step"] = "ask_description"
        prompt = "תאר את התקלה בקצרה:" if "1" in msg or "שירות" in msg or "תקלה" in msg else "מה יש להתקין / מה הבקשה?"
        return f"שאלה 4/5 ➤ *{prompt}*"

    if step == "ask_description":
        if is_no(msg):
            reset_session(phone)
            return "הקריאה בוטלה. שלח *כן* אם תרצה להתחיל מחדש."
        if len(msg) < 3:
            return "נא להזין תיאור קצר."
        session["data"]["description"] = msg
        session["step"] = "ask_phone"
        return "שאלה 5/5 ➤ *מה מספר הטלפון שלך ליצירת קשר?* 📞"

    if step == "ask_phone":
        if is_no(msg):
            reset_session(phone)
            return "הקריאה בוטלה. שלח *כן* אם תרצה להתחיל מחדש."
        digits = re.sub(r"\D", "", msg)
        if len(digits) < 9:
            return "נא להזין מספר טלפון תקין."
        session["data"]["contact_phone"] = msg
        session["step"] = "confirm"
        d = session["data"]
        return (
            "📋 *סיכום הקריאה — אנא אשר:*\n\n"
            f"👤 שם: {d['name']}\n"
            f"📍 כתובת: {d['address']}\n"
            f"🔧 סוג: {d['call_type']}\n"
            f"📝 תיאור: {d['description']}\n"
            f"📞 טלפון: {d['contact_phone']}\n\n"
            "שלח *כן* לאישור, או *לא* לביטול."
        )

    if step == "confirm":
        if is_yes(msg):
            d = session["data"]
            call_id = len(service_calls) + 1
            service_calls.append({
                "id": call_id, "phone": phone,
                "name": d["name"], "address": d["address"],
                "call_type": d["call_type"], "description": d["description"],
                "contact_phone": d["contact_phone"],
                "opened_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
                "status": "ממתינה לטיפול"
            })
            send_message(NOTIFY_PHONE, build_notify_message(call_id, phone, d))
            reset_session(phone)
            return (
                f"✅ *קריאה #{call_id} נפתחה בהצלחה!*\n\n"
                f"נציג יצור איתך קשר בהקדם.\n"
                f"תודה שפנית ל{BUSINESS_NAME}! 🙏\n\n"
                f"לקריאה נוספת — כתוב לי בכל עת 😊"
            )
        if is_no(msg):
            reset_session(phone)
            return "הקריאה בוטלה. שלח *כן* אם תרצה להתחיל מחדש."
        return "שלח *כן* לאישור או *לא* לביטול."

    reset_session(phone)
    return "היי! מעוניין לפתוח קריאת שירות/התקנה? 😊\n\nשלח *כן* להתחלה."


# ─── Webhook ──────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    global global_bot_on
    try:
        data = request.get_json(force=True)
        if not data:
            return "ok"
        if data.get("typeWebhook") != "incomingMessageReceived":
            return "ok"

        msg_data  = data.get("messageData", {})
        body_text = msg_data.get("textMessageData", {}).get("textMessage", "")
        sender    = data.get("senderData", {})
        phone     = sender.get("chatId", "")

        if not phone or not body_text:
            return "ok"

        # סנן קבוצות
        if is_group(phone):
            return "ok"

        phone_clean = phone.replace("@c.us", "")
        add_to_history(phone_clean, "client", body_text)
        sessions.setdefault(phone_clean, {"step": "wait_greeting", "data": {}})

        # בוט פועל רק אם הופעל ידנית על הלקוח הזה
        if bot_enabled.get(phone_clean, False):
            reply = handle_message(phone_clean, body_text)
            add_to_history(phone_clean, "bot", reply)
            send_message(phone_clean, reply)

    except Exception as e:
        print(f"[Webhook] error: {e}")
    return "ok"


# ─── API ──────────────────────────────────────────────────────
@app.route("/api/chats")
def api_chats():
    all_phones = set(list(chat_history.keys()) + list(bot_enabled.keys()))
    result = []
    for phone in all_phones:
        if is_group(phone):
            continue
        history = chat_history.get(phone, [])
        result.append({
            "phone":         phone,
            "bot_active":    bot_enabled.get(phone, False),
            "greeting_sent": greeting_sent.get(phone, False),
            "last_message":  history[-1] if history else None,
            "history":       history,
            "step":          sessions.get(phone, {}).get("step", "wait_greeting")
        })
    result.sort(key=lambda x: x["last_message"]["time"] if x["last_message"] else "00:00", reverse=True)
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
    # שלח הודעת פתיחה בהפעלה הראשונה
    if now_active and not greeting_sent.get(phone, False):
        sent = send_message(phone, GREETING_MSG)
        if sent:
            greeting_sent[phone] = True
            add_to_history(phone, "bot", GREETING_MSG)
            sessions.setdefault(phone, {"step": "wait_greeting", "data": {}})
    return jsonify({"phone": phone, "bot_active": now_active})


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
header{background:var(--s1);border-bottom:1px solid var(--border);padding:0 24px;height:58px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:17px}
.logo-icon{width:34px;height:34px;background:linear-gradient(135deg,var(--accent),#128c7e);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:17px}
.hdr-left{display:flex;align-items:center;gap:16px}
.stats{display:flex;gap:6px}
.stat{background:var(--s2);border:1px solid var(--border);border-radius:8px;padding:5px 14px;font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px}
.stat b{color:var(--text);font-size:15px;font-weight:700}
.btn-global{border:none;border-radius:10px;padding:7px 18px;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:7px}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.main{display:flex;flex:1;overflow:hidden}
.sidebar{width:310px;border-left:1px solid var(--border);background:var(--s1);display:flex;flex-direction:column;flex-shrink:0}
.sb-head{padding:12px 16px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.chat-list{flex:1;overflow-y:auto}
.ci{padding:11px 14px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;align-items:center;gap:10px;transition:background .12s}
.ci:hover{background:var(--s2)}
.ci.active{background:var(--s2);border-right:3px solid var(--accent)}
.ci.disabled{opacity:.5}
.av{width:38px;height:38px;border-radius:50%;background:var(--s3);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;position:relative}
.dot{position:absolute;bottom:-1px;left:-1px;width:12px;height:12px;border-radius:50%;border:2px solid var(--s1);background:var(--muted);transition:all .3s}
.dot.on{background:var(--accent);box-shadow:0 0 6px var(--accent)}
.ci-info{flex:1;min-width:0}
.ci-phone{font-size:12px;font-weight:600;margin-bottom:2px}
.ci-last{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tgl{position:relative;width:36px;height:20px;display:inline-block}
.tgl input{opacity:0;width:0;height:0}
.tsl{position:absolute;cursor:pointer;inset:0;background:var(--border);border-radius:20px;transition:.25s}
.tsl:before{content:"";position:absolute;height:14px;width:14px;right:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
input:checked+.tsl{background:var(--accent)}
input:checked+.tsl:before{transform:translateX(-16px)}
.chat-win{flex:1;display:flex;flex-direction:column;background:var(--bg)}
.topbar{padding:11px 20px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.tb-left{display:flex;align-items:center;gap:10px}
.tb-phone{font-weight:700;font-size:15px}
.tb-sub{font-size:11px;color:var(--muted)}
.badge{padding:4px 11px;border-radius:20px;font-size:11px;font-weight:700;background:var(--s2);color:var(--muted);border:1px solid var(--border)}
.badge.on{background:rgba(37,211,102,.12);color:var(--accent);border-color:rgba(37,211,102,.4)}
.messages{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:65%;padding:9px 13px;border-radius:12px;font-size:13px;line-height:1.55;white-space:pre-wrap;animation:fi .18s ease}
@keyframes fi{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.18);align-self:flex-start;border-bottom-left-radius:3px}
.msg-meta{font-size:10px;color:var(--muted);margin-top:3px}
.msg.client .msg-meta{text-align:right}
.calls-panel{width:270px;border-right:1px solid var(--border);background:var(--s1);display:flex;flex-direction:column;flex-shrink:0}
.cp-head{padding:12px 16px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.calls-list{flex:1;overflow-y:auto;padding:10px}
.call-card{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:11px 12px;margin-bottom:8px;font-size:12px}
.call-id{font-size:10px;color:var(--muted);margin-bottom:4px}
.call-name{font-weight:700;font-size:13px;margin-bottom:3px}
.call-type{color:var(--accent);font-size:11px;margin-bottom:6px}
.call-row{color:var(--muted);margin-bottom:2px}
.call-row span{color:var(--text)}
.status-sel{margin-top:7px;width:100%;background:var(--s3);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text);font-family:inherit;font-size:12px;outline:none;cursor:pointer}
.empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);gap:8px}
.empty-icon{font-size:42px;opacity:.3}
.no-items{padding:30px 12px;text-align:center;color:var(--muted);font-size:12px;line-height:1.6}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <div class="logo"><div class="logo-icon">🔧</div>מרכז שירות לקוחות</div>
    <button class="btn-global on" id="global-btn" onclick="toggleGlobal()">🟢 בוט פעיל לכולם</button>
  </div>
  <div class="stats">
    <div class="stat">שיחות <b id="s1">0</b></div>
    <div class="stat">קריאות <b id="s2">0</b></div>
    <div class="stat">כבויים <b id="s3">0</b></div>
  </div>
</header>
<div class="main">
  <div class="calls-panel">
    <div class="cp-head">קריאות שירות</div>
    <div class="calls-list" id="calls-list"><div class="no-items">אין קריאות עדיין</div></div>
  </div>
  <div class="chat-win" id="win">
    <div class="empty"><div class="empty-icon">💬</div><div>בחר שיחה מהרשימה</div></div>
  </div>
  <div class="sidebar">
    <div class="sb-head">לקוחות</div>
    <div class="chat-list" id="list"><div class="no-items">ממתין להודעות...</div></div>
  </div>
</div>
<script>
let chats=[], calls=[], sel=null, globalOn=true;
const STEPS={"wait_greeting":"ממתין","ask_name":"שם","ask_address":"כתובת","ask_call_type":"סוג","ask_description":"תיאור","ask_phone":"טלפון","confirm":"אישור"};

async function load(){
  const [cr,sr,gr]=await Promise.all([fetch('/api/chats'),fetch('/api/service-calls'),fetch('/api/global-status')]);
  chats=await cr.json(); calls=await sr.json(); const gs=await gr.json();
  globalOn=gs.global_bot_on;
  const btn=document.getElementById('global-btn');
  if(globalOn){btn.className='btn-global on';btn.textContent='🟢 בוט פעיל לכולם';}
  else{btn.className='btn-global off';btn.textContent='🔴 בוט כבוי לכולם';}
  document.getElementById('s1').textContent=chats.length;
  document.getElementById('s2').textContent=calls.length;
  document.getElementById('s3').textContent=chats.filter(c=>!c.bot_active).length;
  renderList(); renderCalls();
  if(sel){const c=chats.find(c=>c.phone===sel);if(c)renderWin(c);}
}

function renderList(){
  const el=document.getElementById('list');
  if(!chats.length){el.innerHTML='<div class="no-items">ממתין להודעות נכנסות...</div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="ci${c.phone===sel?' active':''}${!c.bot_active?' disabled':''}" onclick="pick('${c.phone}')">
      <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
      <div class="ci-info">
        <div class="ci-phone">${fmt(c.phone)}</div>
        <div class="ci-last">${c.last_message?esc(c.last_message.message).substring(0,35)+'...':'ממתין...'}</div>
      </div>
      <div onclick="event.stopPropagation()">
        <label class="tgl" title="${c.bot_active?'כבה בוט ללקוח זה':'הפעל בוט ללקוח זה'}">
          <input type="checkbox"${c.bot_active?' checked':''} onchange="tog('${c.phone}')">
          <span class="tsl"></span>
        </label>
      </div>
    </div>`).join('');
}

function renderCalls(){
  const el=document.getElementById('calls-list');
  if(!calls.length){el.innerHTML='<div class="no-items">אין קריאות עדיין</div>';return;}
  el.innerHTML=[...calls].reverse().map(c=>`
    <div class="call-card">
      <div class="call-id">קריאה #${c.id} · ${c.opened_at}</div>
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
        <div style="font-size:20px">👤</div>
        <div><div class="tb-phone">${fmt(c.phone)}</div><div class="tb-sub">${h.length} הודעות · ${STEPS[c.step]||c.step}</div></div>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="badge${isActive?' on':''}">${isActive?'🤖 פעיל':'⏸ כבוי'}</span>
        <label class="tgl"><input type="checkbox"${c.bot_active?' checked':''} onchange="tog('${c.phone}')"><span class="tsl"></span></label>
      </div>
    </div>
    <div class="messages" id="msgs">
      ${h.length?h.map(m=>`<div class="msg ${m.sender}">${esc(m.message)}<div class="msg-meta">${m.sender==='bot'?'🤖 ':''}${m.time}</div></div>`).join('')
        :'<div style="text-align:center;color:var(--muted);font-size:12px;margin-top:30px">אין הודעות עדיין</div>'}
    </div>`;
  const msgs=document.getElementById('msgs');
  if(msgs)msgs.scrollTop=msgs.scrollHeight;
}

function pick(phone){sel=phone;const c=chats.find(c=>c.phone===phone);if(c)renderWin(c);renderList();}
async function tog(phone){await fetch(`/api/toggle/${phone}`,{method:'POST'});await load();}
async function toggleGlobal(){await fetch('/api/global-toggle',{method:'POST'});await load();}
async function updateStatus(id,status){
  await fetch(`/api/service-calls/${id}/status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function fmt(p){return p.replace('@c.us','').replace(/^972/,'0');}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
load();setInterval(load,3000);
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
.hdr{background:var(--s1);border-bottom:1px solid var(--border);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.hdr-logo{display:flex;align-items:center;gap:9px;font-weight:800;font-size:16px}
.hdr-icon{width:32px;height:32px;background:linear-gradient(135deg,#25d366,#128c7e);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:16px}
.btn-global{border:none;border-radius:20px;padding:6px 14px;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer}
.btn-global.on{background:rgba(37,211,102,.15);color:var(--accent);border:1px solid var(--accent)}
.btn-global.off{background:rgba(231,76,60,.15);color:var(--danger);border:1px solid var(--danger)}
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--border);flex-shrink:0}
.tab{flex:1;padding:12px;text-align:center;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.page{display:none;flex:1;overflow-y:auto;flex-direction:column}
.page.active{display:flex}
.cards{padding:12px;display:flex;flex-direction:column;gap:10px;padding-bottom:20px}
.card{background:var(--s1);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.card.on{border-color:rgba(37,211,102,.35)}
.card.off{opacity:.6}
.card-top{padding:13px 14px;display:flex;align-items:center;gap:11px}
.av{width:40px;height:40px;border-radius:50%;background:var(--s2);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;position:relative}
.dot{position:absolute;bottom:-1px;left:-1px;width:13px;height:13px;border-radius:50%;border:2px solid var(--s1);background:var(--muted)}
.dot.on{background:var(--accent);box-shadow:0 0 7px var(--accent)}
.ci{flex:1;min-width:0}
.ci-phone{font-weight:700;font-size:14px;margin-bottom:2px}
.ci-last{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci-step{font-size:10px;margin-top:3px;color:var(--muted)}
.ci-step.on{color:var(--accent)}
.tgl{position:relative;width:40px;height:22px;display:inline-block;flex-shrink:0}
.tgl input{opacity:0;width:0;height:0}
.tsl{position:absolute;cursor:pointer;inset:0;background:var(--border);border-radius:22px;transition:.25s}
.tsl:before{content:"";position:absolute;height:16px;width:16px;right:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
input:checked+.tsl{background:var(--accent)}
input:checked+.tsl:before{transform:translateX(-18px)}
.chat-btn{width:100%;background:var(--s2);border:none;border-top:1px solid var(--border);padding:10px 14px;color:var(--muted);font-family:inherit;font-size:12px;cursor:pointer;text-align:right;display:flex;align-items:center;justify-content:space-between}
.call-card{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:14px;margin:0 12px 10px}
.call-id{font-size:10px;color:var(--muted);margin-bottom:5px}
.call-name{font-weight:700;font-size:15px;margin-bottom:3px}
.call-type{color:var(--accent);font-size:12px;margin-bottom:8px}
.call-row{font-size:12px;color:var(--muted);margin-bottom:3px}
.call-row span{color:var(--text)}
.status-sel{margin-top:10px;width:100%;background:var(--s3);border:1px solid var(--border);border-radius:10px;padding:8px 12px;color:var(--text);font-family:inherit;font-size:13px;outline:none;cursor:pointer}
.chat-view{display:none;position:fixed;inset:0;background:var(--bg);flex-direction:column;z-index:100}
.chat-view.active{display:flex}
.chat-hdr{padding:12px 16px;background:var(--s1);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.back-btn{background:none;border:none;color:var(--accent);font-size:24px;cursor:pointer;padding:0 2px;line-height:1}
.chat-hdr-phone{font-weight:700;font-size:15px}
.chat-hdr-sub{font-size:11px;color:var(--muted)}
.msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:7px}
.msg{max-width:80%;padding:9px 12px;border-radius:12px;font-size:13px;line-height:1.5;white-space:pre-wrap}
.msg.client{background:var(--s2);border:1px solid var(--border);align-self:flex-end;border-bottom-right-radius:3px}
.msg.bot{background:#172e20;border:1px solid rgba(37,211,102,.2);align-self:flex-start;border-bottom-left-radius:3px}
.msg-time{font-size:9px;color:var(--muted);margin-top:3px}
.msg.client .msg-time{text-align:right}
.empty{padding:50px 20px;text-align:center;color:var(--muted)}
.empty div:first-child{font-size:44px;opacity:.3;margin-bottom:10px}
.sec{padding:12px 14px 6px;font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-logo"><div class="hdr-icon">🔧</div>בוט שירות</div>
  <button class="btn-global on" id="g-btn" onclick="toggleGlobal()">🟢 פעיל לכולם</button>
</div>
<div class="tabs">
  <div class="tab active" onclick="showTab('clients')">👥 לקוחות</div>
  <div class="tab" onclick="showTab('calls')">🔧 קריאות</div>
</div>
<div class="page active" id="page-clients">
  <div class="sec">לקוחות</div>
  <div class="cards" id="cards"></div>
</div>
<div class="page" id="page-calls">
  <div class="sec">קריאות שירות</div>
  <div id="calls-list"></div>
</div>
<div class="chat-view" id="chat-view">
  <div class="chat-hdr">
    <button class="back-btn" onclick="closeChat()">‹</button>
    <div style="flex:1">
      <div class="chat-hdr-phone" id="cv-phone"></div>
      <div class="chat-hdr-sub" id="cv-sub"></div>
    </div>
    <label class="tgl"><input type="checkbox" id="cv-tgl" onchange="cvToggle()"><span class="tsl"></span></label>
  </div>
  <div class="msgs" id="cv-msgs"></div>
</div>
<script>
let chats=[], calls=[], cvPhone=null, globalOn=true;
const STEPS={"wait_greeting":"ממתין","ask_name":"שם","ask_address":"כתובת","ask_call_type":"סוג","ask_description":"תיאור","ask_phone":"טלפון","confirm":"אישור"};

async function load(){
  const [cr,sr,gr]=await Promise.all([fetch('/api/chats'),fetch('/api/service-calls'),fetch('/api/global-status')]);
  chats=await cr.json(); calls=await sr.json(); const gs=await gr.json();
  globalOn=gs.global_bot_on;
  const btn=document.getElementById('g-btn');
  if(globalOn){btn.className='btn-global on';btn.textContent='🟢 פעיל לכולם';}
  else{btn.className='btn-global off';btn.textContent='🔴 כבוי לכולם';}
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
  if(!chats.length){el.innerHTML='<div class="empty"><div>💬</div><div>ממתין להודעות נכנסות</div></div>';return;}
  el.innerHTML=chats.map(c=>`
    <div class="card${c.bot_active?' on':' off'}">
      <div class="card-top">
        <div class="av">👤<div class="dot${c.bot_active&&globalOn?' on':''}"></div></div>
        <div class="ci">
          <div class="ci-phone">${fmt(c.phone)}</div>
          <div class="ci-last">${c.last_message?esc(c.last_message.message).substring(0,40):c.bot_active?'פעיל':'כבוי'}</div>
          <div class="ci-step${c.bot_active?' on':''}">${c.bot_active&&globalOn?'🟢 '+(STEPS[c.step]||c.step):'⚫ כבוי'}</div>
        </div>
        <label class="tgl" onclick="event.stopPropagation()">
          <input type="checkbox"${c.bot_active?' checked':''} onchange="tog('${c.phone}')">
          <span class="tsl"></span>
        </label>
      </div>
      <button class="chat-btn" onclick="openChat('${c.phone}')">
        <span>💬 שיחה (${(c.history||[]).length} הודעות)</span><span>›</span>
      </button>
    </div>`).join('');
}

function renderCalls(){
  const el=document.getElementById('calls-list');
  if(!calls.length){el.innerHTML='<div class="empty"><div>🔧</div><div>אין קריאות עדיין</div></div>';return;}
  el.innerHTML=[...calls].reverse().map(c=>`
    <div class="call-card">
      <div class="call-id">קריאה #${c.id} · ${c.opened_at}</div>
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
    <div class="msg ${m.sender}">${esc(m.message)}<div class="msg-time">${m.sender==='bot'?'🤖 ':''}${m.time}</div></div>`).join('')
    :'<div style="text-align:center;color:var(--muted);font-size:13px;margin-top:30px">אין הודעות</div>';
  msgs.scrollTop=msgs.scrollHeight;
}

function closeChat(){document.getElementById('chat-view').classList.remove('active');cvPhone=null;}
async function cvToggle(){if(cvPhone){await fetch('/api/toggle/'+cvPhone,{method:'POST'});await load();}}
async function tog(phone){await fetch('/api/toggle/'+phone,{method:'POST'});await load();}
async function toggleGlobal(){await fetch('/api/global-toggle',{method:'POST'});await load();}
async function updateStatus(id,status){
  await fetch('/api/service-calls/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  await load();
}
function fmt(p){return p.replace('@c.us','').replace(/^972/,'0');}
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
