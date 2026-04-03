"""
LogiMail — Bulk Personalized Email Sender
Full user auth + per-user dashboards via SQLite
"""
import base64, csv, hashlib, io, json, logging, os, re, secrets
import smtplib, sqlite3, threading, time, uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path

import anthropic, openpyxl
from flask import (Flask, Response, g, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import Template, TemplateError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path("data"); DATA_DIR.mkdir(exist_ok=True)
DB_PATH  = DATA_DIR / "logimail.db"

# ── In-memory campaign state (keyed by campaign_id) ───────────────────────────
_cam_lock  = threading.Lock()
campaigns  = {}   # campaign_id → state dict
opens_db   = {}   # campaign_id → [{"email","time","via"}]
unsubs_db  = {}   # campaign_id → set of emails

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════
def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(e=None):
    db = getattr(g, '_db', None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id        TEXT PRIMARY KEY,
            email     TEXT UNIQUE NOT NULL,
            name      TEXT NOT NULL,
            password  TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS smtp_settings (
            user_id    TEXT PRIMARY KEY,
            from_name  TEXT,
            smtp_host  TEXT,
            smtp_port  INTEGER DEFAULT 587,
            smtp_pass  TEXT,
            delay      REAL DEFAULT 1.5,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS campaign_records (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            status      TEXT,
            total       INTEGER DEFAULT 0,
            sent        INTEGER DEFAULT 0,
            failed      INTEGER DEFAULT 0,
            skipped     INTEGER DEFAULT 0,
            opens       INTEGER DEFAULT 0,
            unsubscribes INTEGER DEFAULT 0,
            created_at  TEXT,
            scheduled_for TEXT,
            attachments TEXT DEFAULT '[]',
            log_json    TEXT DEFAULT '[]',
            subject     TEXT DEFAULT '',
            template_html TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS open_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            email       TEXT NOT NULL,
            via         TEXT,
            opened_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS unsub_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            email       TEXT NOT NULL UNIQUE,
            unsubbed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used        INTEGER DEFAULT 0
        );
        """)
        db.commit()

init_db()

# ── DB Migration — safely add new columns to existing databases ───────────────
def migrate_db():
    """Add new columns to existing DB without destroying data."""
    with app.app_context():
        db = get_db()
        migrations = [
            "ALTER TABLE campaign_records ADD COLUMN subject TEXT DEFAULT ''",
            "ALTER TABLE campaign_records ADD COLUMN template_html TEXT DEFAULT ''",
        ]
        for sql in migrations:
            try:
                db.execute(sql)
                db.commit()
                log.info(f"Migration applied: {sql[:60]}")
            except Exception:
                pass  # Column already exists — safe to ignore

migrate_db()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def current_user():
    uid = session.get('user_id')
    if not uid: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

# ── DB helpers ─────────────────────────────────────────────────────────────────
def save_campaign_to_db(campaign_id, user_id, state):
    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO campaign_records
        (id, user_id, status, total, sent, failed, skipped, opens, unsubscribes,
         created_at, scheduled_for, attachments, log_json, subject, template_html)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (campaign_id, user_id,
          state.get("status"), state.get("total",0),
          state.get("sent",0), state.get("failed",0), state.get("skipped",0),
          state.get("opens",0), state.get("unsubscribes",0),
          state.get("created_at"), state.get("scheduled_for"),
          json.dumps(state.get("attachments",[])),
          json.dumps(state.get("log",[])[-100:]),
          state.get("subject",""),
          state.get("template_html","")))
    db.commit()

def update_campaign_stats(campaign_id, user_id):
    """Sync in-memory state to DB."""
    state = campaigns.get(campaign_id)
    if state: save_campaign_to_db(campaign_id, user_id, state)

def record_open_db(campaign_id, email, via="click"):
    db = get_db()
    already = db.execute(
        "SELECT 1 FROM open_records WHERE campaign_id=? AND email=?",
        (campaign_id, email)).fetchone()
    db.execute(
        "INSERT INTO open_records (campaign_id, email, via, opened_at) VALUES (?,?,?,?)",
        (campaign_id, email, via, datetime.utcnow().isoformat()))
    db.commit()
    unique = db.execute(
        "SELECT COUNT(DISTINCT email) FROM open_records WHERE campaign_id=?",
        (campaign_id,)).fetchone()[0]
    db.execute(
        "UPDATE campaign_records SET opens=? WHERE id=?", (unique, campaign_id))
    db.commit()
    # Update in-memory
    if campaign_id in campaigns:
        campaigns[campaign_id]["opens"] = unique
        if not already:
            campaigns[campaign_id]["log"].append(
                {"type":"info","msg":f"👁 Opened by {email}"})
    return not already

def record_unsub_db(campaign_id, email):
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO unsub_records (campaign_id, email, unsubbed_at) VALUES (?,?,?)",
            (campaign_id, email, datetime.utcnow().isoformat()))
        db.commit()
    except: pass
    count = db.execute(
        "SELECT COUNT(*) FROM unsub_records WHERE campaign_id=?",
        (campaign_id,)).fetchone()[0]
    db.execute(
        "UPDATE campaign_records SET unsubscribes=? WHERE id=?", (count, campaign_id))
    db.commit()
    if campaign_id in campaigns:
        campaigns[campaign_id]["unsubscribes"] = count
        campaigns[campaign_id]["log"].append(
            {"type":"warn","msg":f"⊘ Unsubscribed: {email}"})
    unsubs_db.setdefault(campaign_id, set()).add(email)

# ══════════════════════════════════════════════════════════════════════════════
# FILE PARSERS
# ══════════════════════════════════════════════════════════════════════════════
def parse_csv(content):
    if content.startswith('\ufeff'): content = content[1:]
    reader = csv.DictReader(io.StringIO(content))
    rows = [{k.strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
    return rows, [c.strip() for c in (reader.fieldnames or [])]

def parse_excel(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = list(ws.iter_rows(values_only=True))
    if not rows_iter: return [], []
    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows_iter[0])]
    rows = []
    for row in rows_iter[1:]:
        record = {headers[i]: str(v).strip() if v is not None else "" for i, v in enumerate(row)}
        if any(v for v in record.values()): rows.append(record)
    wb.close()
    return rows, headers

def parse_file(file):
    name = file.filename.lower()
    data = file.read()
    if name.endswith(('.xlsx', '.xls')): return parse_excel(data)
    try: return parse_csv(data.decode('utf-8'))
    except UnicodeDecodeError: return parse_csv(data.decode('latin-1'))

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def detect_email(recipient):
    for key in recipient:
        if key.lower().strip() in ("email","e-mail","emailaddress","email_address","mail"):
            return recipient[key].strip()
    for val in recipient.values():
        if "@" in str(val) and "." in str(val): return str(val).strip()
    return ""

def render_html(tpl, variables):
    """Render Jinja2 template safely. If template has syntax errors, return as plain HTML."""
    try:
        return Template(tpl).render(**variables)
    except TemplateError:
        # Template has invalid syntax (e.g. {{ First Name }})
        # Fall back to simple string replacement so emails still send
        result = tpl
        for key, val in variables.items():
            result = result.replace(f"{{{{{key}}}}}", str(val))
            result = result.replace(f"{{{{ {key} }}}}", str(val))
        return result

def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()

def generate_ai_paragraph(recipient, client):
    msg = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=200,
        messages=[{"role":"user","content":
            f"Write a warm, 2-sentence personalised email opening for "
            f"{recipient.get('first_name','the recipient')} who works at "
            f"{recipient.get('company','their company')} as a "
            f"{recipient.get('role','professional')}. "
            "Be specific, human, no filler. No intro, just the paragraph."}])
    return msg.content[0].text.strip()

def enc(s): return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
def dec(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode()).decode()

def wrap_links(html_body, campaign_id, encoded_email, base_url):
    def replace_href(match):
        url = match.group(1)
        if "/track/" in url or url.startswith("mailto:") or url.strip()=="#":
            return match.group(0)
        return f'href="{base_url}/track/click/{campaign_id}/{encoded_email}/{enc(url)}"'
    return re.sub(r'href="([^"]+)"', replace_href, html_body)

def inject_tracking(html_body, campaign_id, email, base_url):
    encoded_email = enc(email)
    unsub_url    = f"{base_url}/track/unsub/{campaign_id}/{encoded_email}"
    receipt_url  = f"{base_url}/track/receipt/{campaign_id}/{encoded_email}"

    html_body = html_body.replace("{{unsubscribe_link}}", unsub_url)
    html_body = html_body.replace("{{ unsubscribe_link }}", unsub_url)

    # ── Confirm Receipt button (visible, reliable open tracking) ──────────────
    receipt_block = f"""
<div style="text-align:center;padding:20px 16px 8px;font-family:Arial,sans-serif">
  <a href="{receipt_url}" style="display:inline-block;background:#22c55e;color:#ffffff;
     padding:11px 28px;border-radius:7px;text-decoration:none;font-size:13px;
     font-weight:600;letter-spacing:0.3px;box-shadow:0 2px 8px rgba(34,197,94,.35)">
    ✅ Confirm Receipt
  </a>
  <div style="font-size:11px;color:#999;margin-top:6px">
    Click to confirm you received this email
  </div>
</div>"""

    # ── Unsubscribe footer ────────────────────────────────────────────────────
    if "unsubscribe" not in html_body.lower():
        unsub_footer = (
            f'<div style="text-align:center;padding:8px 12px 16px;font-family:Arial,sans-serif">'
            f'<a href="{unsub_url}" style="color:#aaa;font-size:11px;text-decoration:underline">Unsubscribe</a>'
            f'</div>')
    else:
        unsub_footer = ""

    inject = receipt_block + unsub_footer

    if re.search(r"</body>", html_body, re.IGNORECASE):
        html_body = re.sub(r"</body>", f"{inject}</body>", html_body, flags=re.IGNORECASE)
    else:
        html_body += inject

    # Wrap all links for click tracking
    html_body = wrap_links(html_body, campaign_id, encoded_email, base_url)
    return html_body

def build_mime(sender_email, sender_name, subject, to_email, html_body, attachments=None):
    from email.mime.base import MIMEBase
    from email import encoders
    import mimetypes
    if attachments:
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(strip_tags(html_body), "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        for filename, filedata in attachments:
            mime_type, _ = mimetypes.guess_type(filename)
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(filedata)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(strip_tags(html_body), "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    return msg

def send_smtp(cfg, mime_msg):
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.ehlo(); s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.sendmail(cfg["smtp_user"], mime_msg["To"], mime_msg.as_string())

# ══════════════════════════════════════════════════════════════════════════════
# CAMPAIGN RUNNER
# ══════════════════════════════════════════════════════════════════════════════
def run_campaign(campaign_id, user_id, cfg, recipients, template_str,
                 use_ai, dry_run, base_url="", attachments=None):
    with app.app_context():
        state = campaigns[campaign_id]
        state["status"] = "running"
        save_campaign_to_db(campaign_id, user_id, state)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        ai_client = anthropic.Anthropic(api_key=api_key) if (use_ai and api_key) else None
        if use_ai and not api_key:
            state["log"].append({"type":"warn","msg":"⚠️ AI skipped — ANTHROPIC_API_KEY not set."})

        for i, recipient in enumerate(recipients):
            if state.get("cancelled"):
                state["status"] = "cancelled"; break

            email = detect_email(recipient)
            state["current"] = i + 1
            state["current_email"] = email

            db = get_db()
            is_unsubbed = db.execute(
                "SELECT 1 FROM unsub_records WHERE campaign_id=? AND email=?",
                (campaign_id, email)).fetchone()
            if is_unsubbed:
                state["skipped"] += 1
                state["log"].append({"type":"warn","msg":f"Skipped unsubscribed: {email}"}); continue

            if not email or "@" not in email:
                state["skipped"] += 1
                state["log"].append({"type":"warn","msg":f"Row {i+1}: No valid email"}); continue

            try:
                if use_ai and ai_client:
                    recipient["ai_paragraph"] = generate_ai_paragraph(recipient, ai_client)
                subject   = render_html(cfg.get("subject_template","Hello"), recipient)
                html_body = render_html(template_str, recipient)
                if not dry_run:
                    html_body = inject_tracking(html_body, campaign_id, email, base_url)
                mime_msg = build_mime(cfg["smtp_user"], cfg.get("from_name",""),
                                      subject, email, html_body, attachments)
                if dry_run:
                    preview_dir = Path(f"previews/{campaign_id}"); preview_dir.mkdir(parents=True, exist_ok=True)
                    (preview_dir / f"{email.replace('@','_at_').replace('.','_')}.html").write_text(html_body, encoding='utf-8')
                    state["log"].append({"type":"info","msg":f"✓ Preview saved for {email}"})
                else:
                    send_smtp(cfg, mime_msg)
                    state["log"].append({"type":"success","msg":f"✓ Sent to {email}"})
                state["sent"] += 1
            except TemplateError as e:
                state["failed"] += 1
                state["log"].append({"type":"error","msg":f"Template error for {email}: {e} — check for variables with spaces e.g. use {{{{first_name}}}} not {{{{First Name}}}}"})
            except smtplib.SMTPException as e:
                state["failed"] += 1
                state["log"].append({"type":"error","msg":f"SMTP error for {email}: {e}"})
            except Exception as e:
                state["failed"] += 1
                state["log"].append({"type":"error","msg":f"Error for {email}: {e}"})

            if i % 5 == 0: save_campaign_to_db(campaign_id, user_id, state)
            time.sleep(float(cfg.get("delay_seconds", 1.0)))

        if not state.get("cancelled"): state["status"] = "done"
        state["progress"] = 100
        save_campaign_to_db(campaign_id, user_id, state)

def schedule_campaign(campaign_id, user_id, fire_at, *args):
    with app.app_context():
        delay = (fire_at - datetime.utcnow()).total_seconds()
        if delay > 0:
            campaigns[campaign_id]["status"] = "scheduled"
            campaigns[campaign_id]["log"].append(
                {"type":"info","msg":f"⏰ Scheduled for {fire_at.strftime('%Y-%m-%d %H:%M UTC')}"})
            save_campaign_to_db(campaign_id, user_id, campaigns[campaign_id])
            time.sleep(delay)
        if not campaigns.get(campaign_id, {}).get("cancelled"):
            run_campaign(campaign_id, user_id, *args)

# ══════════════════════════════════════════════════════════════════════════════
# AUTH PAGES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/login")
def login_page():
    if 'user_id' in session: return redirect('/')
    return render_template("auth.html", mode="login")

@app.route("/register")
def register_page():
    if 'user_id' in session: return redirect('/')
    return render_template("auth.html", mode="register")

@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.json or {}
    name  = (d.get("name","")).strip()
    email = (d.get("email","")).strip().lower()
    pw    = d.get("password","")
    if not name or not email or not pw:
        return jsonify({"ok":False,"error":"All fields are required"}), 400
    if len(pw) < 6:
        return jsonify({"ok":False,"error":"Password must be at least 6 characters"}), 400
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return jsonify({"ok":False,"error":"An account with this email already exists"}), 400
    uid = str(uuid.uuid4())[:12]
    db.execute("INSERT INTO users (id,email,name,password,created_at) VALUES (?,?,?,?,?)",
               (uid, email, name, hash_pw(pw), datetime.utcnow().isoformat()))
    db.commit()
    session['user_id']   = uid
    session['user_email'] = email
    session['user_name']  = name
    return jsonify({"ok":True,"name":name,"email":email})

@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    email = (d.get("email","")).strip().lower()
    pw    = d.get("password","")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND password=?",
                      (email, hash_pw(pw))).fetchone()
    if not user:
        return jsonify({"ok":False,"error":"Invalid email or password"}), 401
    session['user_id']   = user['id']
    session['user_email'] = user['email']
    session['user_name']  = user['name']
    return jsonify({"ok":True,"name":user['name'],"email":user['email']})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok":True})

@app.route("/api/auth/me")
def me():
    if 'user_id' not in session:
        return jsonify({"ok":False}), 401
    return jsonify({"ok":True,"name":session.get('user_name'),
                    "email":session.get('user_email')})

# ══════════════════════════════════════════════════════════════════════════════
# SMTP SETTINGS (saved per user)
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/smtp-settings", methods=["GET"])
@login_required
def get_smtp():
    db = get_db()
    row = db.execute("SELECT * FROM smtp_settings WHERE user_id=?",
                     (session['user_id'],)).fetchone()
    if not row:
        return jsonify({"ok":True,"settings":{}})
    return jsonify({"ok":True,"settings":{
        "from_name": row['from_name'],
        "smtp_host": row['smtp_host'],
        "smtp_port": row['smtp_port'],
        "delay":     row['delay'],
        # Never return the password
    }})

@app.route("/api/smtp-settings", methods=["POST"])
@login_required
def save_smtp():
    d = request.json or {}
    db = get_db()
    existing = db.execute("SELECT 1 FROM smtp_settings WHERE user_id=?",
                           (session['user_id'],)).fetchone()
    if existing:
        # Only update password if provided
        if d.get("smtp_pass"):
            db.execute("""UPDATE smtp_settings SET
                from_name=?,smtp_host=?,smtp_port=?,smtp_pass=?,delay=? WHERE user_id=?""",
                (d.get("from_name"), d.get("smtp_host"), d.get("smtp_port",587),
                 d.get("smtp_pass"), d.get("delay",1.5), session['user_id']))
        else:
            db.execute("""UPDATE smtp_settings SET
                from_name=?,smtp_host=?,smtp_port=?,delay=? WHERE user_id=?""",
                (d.get("from_name"), d.get("smtp_host"), d.get("smtp_port",587),
                 d.get("delay",1.5), session['user_id']))
    else:
        db.execute("""INSERT INTO smtp_settings (user_id,from_name,smtp_host,smtp_port,smtp_pass,delay)
            VALUES (?,?,?,?,?,?)""",
            (session['user_id'], d.get("from_name"), d.get("smtp_host"),
             d.get("smtp_port",587), d.get("smtp_pass"), d.get("delay",1.5)))
    db.commit()
    return jsonify({"ok":True})

# ══════════════════════════════════════════════════════════════════════════════
# TRACKING ROUTES (no login required — accessed from emails)
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/track/open/<campaign_id>/<encoded_email>")
def track_open(campaign_id, encoded_email):
    try:
        with app.app_context():
            email = dec(encoded_email)
            record_open_db(campaign_id, email, via="pixel")
    except Exception as e: log.warning(f"track_open: {e}")
    return Response(TRACKING_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control":"no-store,no-cache","Pragma":"no-cache"})

@app.route("/track/click/<campaign_id>/<encoded_email>/<encoded_url>")
def track_click(campaign_id, encoded_email, encoded_url):
    original_url = "/"
    try:
        with app.app_context():
            email = dec(encoded_email)
            original_url = dec(encoded_url)
            record_open_db(campaign_id, email, via="click")
    except Exception as e: log.warning(f"track_click: {e}")
    return redirect(original_url, code=302)

@app.route("/track/unsub/<campaign_id>/<encoded_email>")
def track_unsub(campaign_id, encoded_email):
    try:
        with app.app_context():
            email = dec(encoded_email)
            record_open_db(campaign_id, email, via="unsubscribe")
            record_unsub_db(campaign_id, email)
    except Exception as e: log.warning(f"track_unsub: {e}")
    return """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Unsubscribed</title>
<style>*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0b0f0c;display:flex;
     align-items:center;justify-content:center;min-height:100vh}
.card{background:#121712;border:1px solid #2a3a2c;border-radius:16px;
      padding:48px 40px;text-align:center;max-width:400px;width:90%}
.icon{font-size:48px;margin-bottom:16px}
h2{color:#22c55e;font-size:22px;margin-bottom:8px;font-weight:700}
p{color:#6b8a6e;font-size:14px;line-height:1.6}</style></head>
<body><div class="card"><div class="icon">✅</div>
<h2>Successfully Unsubscribed</h2>
<p>You have been removed from this mailing list.</p>
</div></body></html>"""

# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/preview-template", methods=["POST"])
@login_required
def preview_template():
    data = request.json
    try:
        return jsonify({"ok":True,"html":render_html(data.get("template",""),data.get("sample",{}))})
    except TemplateError as e:
        return jsonify({"ok":False,"error":str(e)}), 400

# ── Server-side recipient store (keyed by upload_id) ─────────────────────────
recipient_store: dict[str, list] = {}

@app.route("/api/parse-csv", methods=["POST"])
@login_required
def parse_csv_route():
    file = request.files.get("file")
    if not file: return jsonify({"ok":False,"error":"No file"}), 400
    try:
        rows, columns = parse_file(file)
        if not rows: return jsonify({"ok":False,"error":"File appears empty"}), 400
        # Store recipients server-side — only send sample + metadata to client
        upload_id = str(uuid.uuid4())[:12]
        recipient_store[upload_id] = rows
        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "columns": columns,
            "count": len(rows),
            "sample": rows[:3],
            # Still send full rows for small lists (under 500) for backward compat
            "rows": rows if len(rows) <= 500 else []
        })
    except Exception as e:
        return jsonify({"ok":False,"error":f"Could not read file: {e}"}), 400

@app.route("/api/launch", methods=["POST"])
@login_required
def launch():
    import json as _json
    if request.content_type and "multipart" in request.content_type:
        data        = _json.loads(request.form.get("data","{}"))
        attachments = [(f.filename, f.read()) for f in request.files.getlist("attachments") if f.filename]
    else:
        data        = request.json or {}
        attachments = []

    cfg          = data.get("smtp",{})
    template_str = data.get("template","")
    use_ai       = data.get("use_ai",False)
    dry_run      = data.get("dry_run",True)
    sched        = data.get("scheduled_at",None)
    user_id      = session['user_id']
    user_email   = session['user_email']

    # Get recipients — prefer server-side store, fall back to inline
    upload_id  = data.get("upload_id")
    recipients = recipient_store.get(upload_id, []) if upload_id else []
    if not recipients:
        recipients = data.get("recipients", [])

    if not recipients:   return jsonify({"ok":False,"error":"No recipients. Please re-upload your file."}), 400
    if not template_str: return jsonify({"ok":False,"error":"No template"}), 400

    # Use account email as sender
    cfg["smtp_user"] = user_email

    # Load saved SMTP password if not provided
    if not cfg.get("smtp_password"):
        db = get_db()
        row = db.execute("SELECT smtp_pass FROM smtp_settings WHERE user_id=?", (user_id,)).fetchone()
        if row and row['smtp_pass']: cfg["smtp_password"] = row['smtp_pass']

    # Save SMTP settings for next time
    if cfg.get("smtp_host"):
        db = get_db()
        existing = db.execute("SELECT 1 FROM smtp_settings WHERE user_id=?",(user_id,)).fetchone()
        if existing:
            if cfg.get("smtp_password"):
                db.execute("UPDATE smtp_settings SET from_name=?,smtp_host=?,smtp_port=?,smtp_pass=?,delay=? WHERE user_id=?",
                    (cfg.get("from_name"),cfg.get("smtp_host"),cfg.get("smtp_port",587),
                     cfg.get("smtp_password"),cfg.get("delay_seconds",1.5),user_id))
            else:
                db.execute("UPDATE smtp_settings SET from_name=?,smtp_host=?,smtp_port=?,delay=? WHERE user_id=?",
                    (cfg.get("from_name"),cfg.get("smtp_host"),cfg.get("smtp_port",587),
                     cfg.get("delay_seconds",1.5),user_id))
        else:
            db.execute("INSERT INTO smtp_settings (user_id,from_name,smtp_host,smtp_port,smtp_pass,delay) VALUES (?,?,?,?,?,?)",
                (user_id,cfg.get("from_name"),cfg.get("smtp_host"),cfg.get("smtp_port",587),
                 cfg.get("smtp_password"),cfg.get("delay_seconds",1.5)))
        db.commit()

    campaign_id = str(uuid.uuid4())[:8]
    state = {
        "status":"pending","total":len(recipients),
        "sent":0,"failed":0,"skipped":0,"opens":0,"unsubscribes":0,
        "current":0,"current_email":"","progress":0,"log":[],
        "cancelled":False,"scheduled_for":sched,
        "created_at":datetime.utcnow().isoformat(),
        "attachments":[name for name,_ in attachments],
        "subject": cfg.get("subject_template",""),
        "template_html": template_str,
    }
    if attachments:
        state["log"].append({"type":"info","msg":f"📎 {len(attachments)} attachment(s): {', '.join(n for n,_ in attachments)}"})

    campaigns[campaign_id] = state
    save_campaign_to_db(campaign_id, user_id, state)
    base_url = request.host_url.rstrip("/")

    if sched:
        try:
            fire_at = datetime.fromisoformat(sched.replace("Z",""))
            t = threading.Thread(target=schedule_campaign,
                args=(campaign_id, user_id, fire_at, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments),
                daemon=True)
        except ValueError:
            return jsonify({"ok":False,"error":"Invalid schedule time"}), 400
    else:
        t = threading.Thread(target=run_campaign,
            args=(campaign_id, user_id, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments),
            daemon=True)
    t.start()
    return jsonify({"ok":True,"campaign_id":campaign_id})

@app.route("/api/status/<campaign_id>")
@login_required
def status(campaign_id):
    # First check in-memory (running campaigns)
    state = campaigns.get(campaign_id)
    if not state:
        # Fall back to DB
        db = get_db()
        row = db.execute("SELECT * FROM campaign_records WHERE id=? AND user_id=?",
                         (campaign_id, session['user_id'])).fetchone()
        if not row: return jsonify({"ok":False,"error":"Not found"}), 404
        state = dict(row)
        state['log'] = json.loads(row['log_json'] or '[]')
        state['attachments'] = json.loads(row['attachments'] or '[]')
    total = state.get("total",0)
    done  = state.get("sent",0)+state.get("failed",0)+state.get("skipped",0)
    state["progress"] = int((done/total)*100) if total else 0
    # Freshen opens/unsubs from DB
    db = get_db()
    opens = db.execute("SELECT COUNT(DISTINCT email) FROM open_records WHERE campaign_id=?",(campaign_id,)).fetchone()[0]
    unsubs = db.execute("SELECT COUNT(*) FROM unsub_records WHERE campaign_id=?",(campaign_id,)).fetchone()[0]
    state["opens"] = opens; state["unsubscribes"] = unsubs
    return jsonify({"ok":True,**state})

@app.route("/api/cancel/<campaign_id>", methods=["POST"])
@login_required
def cancel(campaign_id):
    state = campaigns.get(campaign_id)
    if state: state["cancelled"] = True
    return jsonify({"ok":True})

@app.route("/api/campaigns")
@login_required
def list_campaigns():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM campaign_records WHERE user_id=? ORDER BY created_at DESC",
        (session['user_id'],)).fetchall()
    result = []
    for row in rows:
        opens  = db.execute("SELECT COUNT(DISTINCT email) FROM open_records WHERE campaign_id=?",(row['id'],)).fetchone()[0]
        unsubs = db.execute("SELECT COUNT(*) FROM unsub_records WHERE campaign_id=?",(row['id'],)).fetchone()[0]
        # Merge with live in-memory state if running
        live = campaigns.get(row['id'],{})
        result.append({
            "id":row['id'],
            "status": live.get("status",row['status']),
            "total":  live.get("total", row['total']),
            "sent":   live.get("sent",  row['sent']),
            "failed": live.get("failed",row['failed']),
            "opens":  opens, "unsubscribes": unsubs,
            "created_at":   row['created_at'],
            "scheduled_for":row['scheduled_for'],
        })
    return jsonify({"ok":True,"campaigns":result})

@app.route("/api/campaigns/<campaign_id>", methods=["DELETE"])
@login_required
def delete_campaign(campaign_id):
    db = get_db()
    row = db.execute("SELECT 1 FROM campaign_records WHERE id=? AND user_id=?",
                     (campaign_id, session['user_id'])).fetchone()
    if not row: return jsonify({"ok":False,"error":"Campaign not found"}), 404
    live = campaigns.get(campaign_id,{})
    if live.get("status") == "running":
        return jsonify({"ok":False,"error":"Stop the campaign before deleting"}), 400
    db.execute("DELETE FROM campaign_records WHERE id=?",(campaign_id,))
    db.execute("DELETE FROM open_records WHERE campaign_id=?",(campaign_id,))
    db.execute("DELETE FROM unsub_records WHERE campaign_id=?",(campaign_id,))
    db.commit()
    campaigns.pop(campaign_id,None)
    return jsonify({"ok":True})


# ── Confirm Receipt (visible button click tracking) ───────────────────────────
@app.route("/track/receipt/<campaign_id>/<encoded_email>")
def track_receipt(campaign_id, encoded_email):
    """Tracks a confirmed open via button click — most reliable method."""
    try:
        with app.app_context():
            email = dec(encoded_email)
            record_open_db(campaign_id, email, via="receipt")
            log.info(f"Receipt confirmed: {email} for campaign {campaign_id}")
    except Exception as e:
        log.warning(f"track_receipt: {e}")
    return """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Receipt Confirmed</title>
<style>*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0b0f0c;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#121712;border:1px solid #2a3a2c;border-radius:16px;
      padding:48px 40px;text-align:center;max-width:400px;width:90%;
      box-shadow:0 8px 40px rgba(0,0,0,.4)}
.icon{font-size:56px;margin-bottom:16px}
h2{color:#22c55e;font-size:24px;margin-bottom:8px;font-weight:700}
p{color:#6b8a6e;font-size:14px;line-height:1.6}</style></head>
<body><div class="card">
  <div class="icon">✅</div>
  <h2>Receipt Confirmed!</h2>
  <p>Thank you for confirming you received this email.<br/>You may now close this tab.</p>
</div></body></html>"""


# ── Password Reset ────────────────────────────────────────────────────────────
@app.route("/forgot-password")
def forgot_password_page():
    return render_template("auth.html", mode="forgot")

@app.route("/reset-password")
def reset_password_page():
    token = request.args.get("token","")
    if not token: return redirect("/login")
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_resets WHERE token=? AND used=0",
        (token,)).fetchone()
    if not row: return render_template("auth.html", mode="reset_invalid")
    # Check expiry
    from datetime import timezone
    expires = datetime.fromisoformat(row['expires_at'])
    if datetime.utcnow() > expires:
        return render_template("auth.html", mode="reset_expired")
    return render_template("auth.html", mode="reset", token=token)

@app.route("/api/auth/forgot-password", methods=["POST"])
def forgot_password():
    d = request.json or {}
    email = (d.get("email","")).strip().lower()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        return jsonify({"ok":True, "message":"If that email exists, a reset link has been sent.", "no_user":True})

    from datetime import timedelta
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    db.execute("INSERT INTO password_resets (token, user_id, expires_at) VALUES (?,?,?)",
               (token, user['id'], expires))
    db.commit()

    base_url = request.host_url.rstrip("/")
    reset_link = f"{base_url}/reset-password?token={token}"

    # Try to send via user's saved SMTP
    email_sent = False
    smtp_row = db.execute("SELECT * FROM smtp_settings WHERE user_id=?",
                          (user['id'],)).fetchone()
    if smtp_row and smtp_row['smtp_host'] and smtp_row['smtp_pass']:
        try:
            html_body = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px;margin:0">
<div style="max-width:500px;margin:0 auto;background:#fff;border-radius:10px;padding:36px 40px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
  <div style="text-align:center;margin-bottom:20px">
    <div style="background:#22c55e;display:inline-block;width:48px;height:48px;border-radius:12px;line-height:48px;font-size:24px;color:#fff">🔑</div>
  </div>
  <h2 style="color:#111;margin:0 0 8px;font-size:20px;text-align:center">Reset Your Password</h2>
  <p style="color:#555;line-height:1.7;margin-bottom:20px;text-align:center">Hi <strong>{user['name']}</strong>, click the button below to reset your LogiMail password. This link expires in <strong>1 hour</strong>.</p>
  <div style="text-align:center;margin:28px 0">
    <a href="{reset_link}" style="background:#22c55e;color:#fff;padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;display:inline-block">
      Reset My Password
    </a>
  </div>
  <p style="color:#999;font-size:12px;text-align:center">If you didn't request this, you can safely ignore this email.</p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0"/>
  <p style="color:#bbb;font-size:11px;text-align:center">Or copy this link: {reset_link}</p>
</div></body></html>"""
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Reset your LogiMail password"
            msg["From"] = f"{smtp_row['from_name'] or 'LogiMail'} <{user['email']}>"
            msg["To"] = user['email']
            msg.attach(MIMEText(f"Reset your LogiMail password.\n\nClick here: {reset_link}\n\nThis link expires in 1 hour.", "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            with smtplib.SMTP(smtp_row['smtp_host'], int(smtp_row['smtp_port'] or 587)) as s:
                s.ehlo(); s.starttls()
                s.login(user['email'], smtp_row['smtp_pass'])
                s.sendmail(user['email'], user['email'], msg.as_string())
            email_sent = True
            log.info(f"Password reset email sent to {email}")
        except Exception as e:
            log.warning(f"Password reset email failed: {e}")

    # Always return the reset link so user can use it even if email fails
    return jsonify({
        "ok": True,
        "email_sent": email_sent,
        "reset_link": reset_link,
        "message": "Reset link sent to your email!" if email_sent else "Could not send email — use the link below to reset your password."
    })

@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    d = request.json or {}
    token = d.get("token","")
    new_pw = d.get("password","")
    if len(new_pw) < 6:
        return jsonify({"ok":False,"error":"Password must be at least 6 characters"}), 400
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_resets WHERE token=? AND used=0", (token,)).fetchone()
    if not row: return jsonify({"ok":False,"error":"Invalid or expired reset link"}), 400
    expires = datetime.fromisoformat(row['expires_at'])
    if datetime.utcnow() > expires:
        return jsonify({"ok":False,"error":"Reset link has expired"}), 400
    db.execute("UPDATE users SET password=? WHERE id=?",
               (hash_pw(new_pw), row['user_id']))
    db.execute("UPDATE password_resets SET used=1 WHERE token=?", (token,))
    db.commit()
    return jsonify({"ok":True})


# ── Campaign Duplication ──────────────────────────────────────────────────────
@app.route("/api/campaigns/<campaign_id>/duplicate", methods=["POST"])
@login_required
def duplicate_campaign(campaign_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM campaign_records WHERE id=? AND user_id=?",
        (campaign_id, session['user_id'])).fetchone()
    if not row: return jsonify({"ok":False,"error":"Campaign not found"}), 404
    return jsonify({
        "ok": True,
        "subject": row['subject'] or "",
        "template_html": row['template_html'] or "",
    })


# ── Analytics ─────────────────────────────────────────────────────────────────
@app.route("/api/analytics/<campaign_id>")
@login_required
def campaign_analytics(campaign_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM campaign_records WHERE id=? AND user_id=?",
        (campaign_id, session['user_id'])).fetchone()
    if not row: return jsonify({"ok":False,"error":"Not found"}), 404

    opens = db.execute(
        "SELECT COUNT(DISTINCT email) FROM open_records WHERE campaign_id=?",
        (campaign_id,)).fetchone()[0]
    unsubs = db.execute(
        "SELECT COUNT(*) FROM unsub_records WHERE campaign_id=?",
        (campaign_id,)).fetchone()[0]

    # Opens over time (hourly buckets)
    open_events = db.execute(
        "SELECT opened_at FROM open_records WHERE campaign_id=? ORDER BY opened_at",
        (campaign_id,)).fetchall()
    open_via = db.execute(
        "SELECT via, COUNT(*) as cnt FROM open_records WHERE campaign_id=? GROUP BY via",
        (campaign_id,)).fetchall()

    live = campaigns.get(campaign_id, {})
    sent    = live.get("sent",    row['sent'])
    failed  = live.get("failed",  row['failed'])
    skipped = live.get("skipped", row['skipped'])
    total   = live.get("total",   row['total'])

    return jsonify({
        "ok": True,
        "id": campaign_id,
        "status": live.get("status", row['status']),
        "total": total, "sent": sent, "failed": failed,
        "skipped": skipped, "opens": opens, "unsubscribes": unsubs,
        "open_rate": round(opens/sent*100, 1) if sent > 0 else 0,
        "open_events": [e['opened_at'] for e in open_events],
        "open_via": {r['via']: r['cnt'] for r in open_via},
        "created_at": row['created_at'],
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)