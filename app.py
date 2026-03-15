"""
LogiMail — Bulk Personalized Email Sender
Persistent tracking via JSON files (survives server restarts/sleep)
"""

import base64
import csv
import io
import json
import logging
import os
import re
import smtplib
import threading
import time
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import openpyxl
from flask import Flask, Response, jsonify, redirect, render_template, request
from jinja2 import Template, TemplateError

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── Persistent storage paths ──────────────────────────────────────────────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CAMPAIGNS_FILE = DATA_DIR / "campaigns.json"
OPENS_FILE     = DATA_DIR / "opens.json"
UNSUBS_FILE    = DATA_DIR / "unsubs.json"

# ── File-based DB helpers ─────────────────────────────────────────────────────
_lock = threading.Lock()

def _read(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default

def _write(path, data):
    with _lock:
        path.write_text(json.dumps(data, default=str))

def load_campaigns():   return _read(CAMPAIGNS_FILE, {})
def save_campaigns(d):  _write(CAMPAIGNS_FILE, d)
def load_opens():       return _read(OPENS_FILE, {})
def save_opens(d):      _write(OPENS_FILE, d)
def load_unsubs():      return {k: set(v) for k, v in _read(UNSUBS_FILE, {}).items()}
def save_unsubs(d):     _write(UNSUBS_FILE, {k: list(v) for k, v in d.items()})

# ── In-memory cache (backed by files) ────────────────────────────────────────
campaigns = load_campaigns()
opens_db  = load_opens()
unsubs_db = load_unsubs()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# ── File parsers ──────────────────────────────────────────────────────────────
def parse_csv(content):
    if content.startswith('\ufeff'):
        content = content[1:]
    reader = csv.DictReader(io.StringIO(content))
    rows = [{k.strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
    return rows, [c.strip() for c in (reader.fieldnames or [])]

def parse_excel(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = list(ws.iter_rows(values_only=True))
    if not rows_iter:
        return [], []
    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows_iter[0])]
    rows = []
    for row in rows_iter[1:]:
        record = {headers[i]: str(v).strip() if v is not None else "" for i, v in enumerate(row)}
        if any(v for v in record.values()):
            rows.append(record)
    wb.close()
    return rows, headers

def parse_file(file):
    name = file.filename.lower()
    data = file.read()
    if name.endswith(('.xlsx', '.xls')):
        return parse_excel(data)
    try:
        return parse_csv(data.decode('utf-8'))
    except UnicodeDecodeError:
        return parse_csv(data.decode('latin-1'))

# ── Email helpers ─────────────────────────────────────────────────────────────
def detect_email(recipient):
    for key in recipient:
        if key.lower().strip() in ("email", "e-mail", "emailaddress", "email_address", "mail"):
            return recipient[key].strip()
    for val in recipient.values():
        if "@" in str(val) and "." in str(val):
            return str(val).strip()
    return ""

def render_html(tpl, variables):
    return Template(tpl).render(**variables)

def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()

def generate_ai_paragraph(recipient, client):
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content":
            f"Write a warm, 2-sentence personalised email opening for "
            f"{recipient.get('first_name','the recipient')} who works at "
            f"{recipient.get('company','their company')} as a "
            f"{recipient.get('role','professional')}. "
            "Be specific, human, no filler. No intro, just the paragraph."
        }],
    )
    return msg.content[0].text.strip()

def enc(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def dec(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode()).decode()

def wrap_links(html_body, campaign_id, encoded_email, base_url):
    def replace_href(match):
        url = match.group(1)
        if "/track/" in url or url.startswith("mailto:") or url.strip() == "#":
            return match.group(0)
        return f'href="{base_url}/track/click/{campaign_id}/{encoded_email}/{enc(url)}"'
    return re.sub(r'href="([^"]+)"', replace_href, html_body)

def inject_tracking(html_body, campaign_id, email, base_url):
    encoded_email = enc(email)
    unsub_url = f"{base_url}/track/unsub/{campaign_id}/{encoded_email}"

    html_body = html_body.replace("{{unsubscribe_link}}", unsub_url)
    html_body = html_body.replace("{{ unsubscribe_link }}", unsub_url)

    if "unsubscribe" not in html_body.lower():
        footer = (
            f'<div style="text-align:center;padding:16px 12px 8px;font-family:Arial,sans-serif">'
            f'<a href="{unsub_url}" style="color:#888;font-size:11px;text-decoration:underline">Unsubscribe</a>'
            f'</div>'
        )
        html_body = re.sub(r"</body>", f"{footer}</body>", html_body, flags=re.IGNORECASE) \
                    if re.search(r"</body>", html_body, re.IGNORECASE) else html_body + footer

    # Wrap all links for click tracking (records open on any click)
    html_body = wrap_links(html_body, campaign_id, encoded_email, base_url)
    return html_body

def build_mime(sender_email, sender_name, subject, to_email, html_body, attachments=None):
    """Build MIME email, optionally with file attachments."""
    from email.mime.base import MIMEBase
    from email import encoders
    import mimetypes

    # Use 'mixed' if attachments exist, else 'alternative'
    if attachments:
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(strip_tags(html_body), "plain"))
        alt.attach(MIMEText(html_body, "html"))
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
        msg.attach(MIMEText(strip_tags(html_body), "plain"))
        msg.attach(MIMEText(html_body, "html"))

    msg["Subject"] = subject
    msg["From"]    = f"{sender_name} <{sender_email}>"
    msg["To"]      = to_email
    return msg

def send_smtp(cfg, mime_msg):
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.ehlo(); s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.sendmail(cfg["smtp_user"], mime_msg["To"], mime_msg.as_string())

# ── Campaign runner ───────────────────────────────────────────────────────────
def record_open(campaign_id, email, via="click"):
    """Record an open event and persist it."""
    opens = load_opens()
    if campaign_id not in opens:
        opens[campaign_id] = []
    already = any(o["email"] == email for o in opens[campaign_id])
    opens[campaign_id].append({"email": email, "time": datetime.utcnow().isoformat(), "via": via})
    save_opens(opens)
    opens_db[campaign_id] = opens[campaign_id]

    # Update campaign stats
    c = load_campaigns()
    if campaign_id in c:
        unique = len(set(o["email"] for o in opens[campaign_id]))
        c[campaign_id]["opens"] = unique
        if not already:
            c[campaign_id]["log"].append({"type": "info", "msg": f"👁 Opened by {email}"})
        save_campaigns(c)
        campaigns[campaign_id] = c[campaign_id]
    return not already

def record_unsub(campaign_id, email):
    """Record unsubscribe and persist it."""
    unsubs = load_unsubs()
    unsubs.setdefault(campaign_id, set()).add(email)
    save_unsubs(unsubs)
    unsubs_db[campaign_id] = unsubs[campaign_id]

    c = load_campaigns()
    if campaign_id in c:
        c[campaign_id]["unsubscribes"] = len(unsubs[campaign_id])
        c[campaign_id]["log"].append({"type": "warn", "msg": f"⊘ Unsubscribed: {email}"})
        save_campaigns(c)
        campaigns[campaign_id] = c[campaign_id]

def run_campaign(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url="", attachments=None):
    c = load_campaigns()
    c[campaign_id]["status"] = "running"
    save_campaigns(c)
    campaigns[campaign_id] = c[campaign_id]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = anthropic.Anthropic(api_key=api_key) if (use_ai and api_key) else None
    if use_ai and not api_key:
        c[campaign_id]["log"].append({"type": "warn", "msg": "⚠️ AI skipped — ANTHROPIC_API_KEY not set."})

    for i, recipient in enumerate(recipients):
        c = load_campaigns()
        if c.get(campaign_id, {}).get("cancelled"):
            c[campaign_id]["status"] = "cancelled"
            save_campaigns(c)
            campaigns[campaign_id] = c[campaign_id]
            break

        email = detect_email(recipient)
        c[campaign_id]["current"] = i + 1
        c[campaign_id]["current_email"] = email

        unsubs = load_unsubs()
        if email in unsubs.get(campaign_id, set()):
            c[campaign_id]["skipped"] = c[campaign_id].get("skipped", 0) + 1
            c[campaign_id]["log"].append({"type": "warn", "msg": f"Skipped unsubscribed: {email}"})
            save_campaigns(c)
            campaigns[campaign_id] = c[campaign_id]
            continue

        if not email or "@" not in email:
            c[campaign_id]["skipped"] = c[campaign_id].get("skipped", 0) + 1
            c[campaign_id]["log"].append({"type": "warn", "msg": f"Row {i+1}: No valid email. Columns: {list(recipient.keys())}"})
            save_campaigns(c)
            campaigns[campaign_id] = c[campaign_id]
            continue

        try:
            if use_ai and ai_client:
                recipient["ai_paragraph"] = generate_ai_paragraph(recipient, ai_client)

            subject   = render_html(cfg.get("subject_template", "Hello"), recipient)
            html_body = render_html(template_str, recipient)

            if not dry_run:
                html_body = inject_tracking(html_body, campaign_id, email, base_url)

            mime_msg = build_mime(cfg["smtp_user"], cfg.get("from_name", ""), subject, email, html_body, attachments)

            if dry_run:
                preview_dir = Path(f"previews/{campaign_id}")
                preview_dir.mkdir(parents=True, exist_ok=True)
                safe = email.replace("@", "_at_").replace(".", "_")
                (preview_dir / f"{safe}.html").write_text(html_body)
                c[campaign_id]["log"].append({"type": "info", "msg": f"✓ Preview saved for {email}"})
            else:
                send_smtp(cfg, mime_msg)
                c[campaign_id]["log"].append({"type": "success", "msg": f"✓ Sent to {email}"})

            c[campaign_id]["sent"] = c[campaign_id].get("sent", 0) + 1

        except TemplateError as e:
            c[campaign_id]["failed"] = c[campaign_id].get("failed", 0) + 1
            c[campaign_id]["log"].append({"type": "error", "msg": f"Template error for {email}: {e}"})
        except smtplib.SMTPException as e:
            c[campaign_id]["failed"] = c[campaign_id].get("failed", 0) + 1
            c[campaign_id]["log"].append({"type": "error", "msg": f"SMTP error for {email}: {e}"})
        except Exception as e:
            c[campaign_id]["failed"] = c[campaign_id].get("failed", 0) + 1
            c[campaign_id]["log"].append({"type": "error", "msg": f"Error for {email}: {e}"})

        save_campaigns(c)
        campaigns[campaign_id] = c[campaign_id]
        time.sleep(float(cfg.get("delay_seconds", 1.0)))

    c = load_campaigns()
    if not c.get(campaign_id, {}).get("cancelled"):
        c[campaign_id]["status"] = "done"
    c[campaign_id]["progress"] = 100
    save_campaigns(c)
    campaigns[campaign_id] = c[campaign_id]

def schedule_campaign(campaign_id, fire_at, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments=None):
    delay = (fire_at - datetime.utcnow()).total_seconds()
    if delay > 0:
        c = load_campaigns()
        c[campaign_id]["status"] = "scheduled"
        c[campaign_id]["log"].append({"type": "info", "msg": f"⏰ Scheduled for {fire_at.strftime('%Y-%m-%d %H:%M UTC')}"})
        save_campaigns(c)
        campaigns[campaign_id] = c[campaign_id]
        time.sleep(delay)
    c = load_campaigns()
    if not c.get(campaign_id, {}).get("cancelled"):
        run_campaign(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments)

# ── Tracking routes ───────────────────────────────────────────────────────────
@app.route("/track/open/<campaign_id>/<encoded_email>")
def track_open(campaign_id, encoded_email):
    try:
        email = dec(encoded_email)
        record_open(campaign_id, email, via="pixel")
    except Exception as e:
        log.warning(f"track_open: {e}")
    return Response(TRACKING_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"})

@app.route("/track/click/<campaign_id>/<encoded_email>/<encoded_url>")
def track_click(campaign_id, encoded_email, encoded_url):
    original_url = "/"
    try:
        email = dec(encoded_email)
        original_url = dec(encoded_url)
        record_open(campaign_id, email, via="click")
        log.info(f"Click: {email} → {original_url}")
    except Exception as e:
        log.warning(f"track_click: {e}")
    return redirect(original_url, code=302)

@app.route("/track/unsub/<campaign_id>/<encoded_email>")
def track_unsub(campaign_id, encoded_email):
    try:
        email = dec(encoded_email)
        # Unsubscribe = definite open, record it first
        record_open(campaign_id, email, via="unsubscribe")
        record_unsub(campaign_id, email)
        log.info(f"Unsub + open recorded: {email}")
    except Exception as e:
        log.warning(f"track_unsub: {e}")
    return """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Unsubscribed</title>
<style>*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#0a0f0b;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#111a13;border:1px solid #243028;border-radius:16px;
      padding:48px 40px;text-align:center;max-width:400px;width:90%}
.icon{font-size:48px;margin-bottom:16px}
h2{color:#3ecf6e;font-size:22px;margin-bottom:8px;font-weight:600}
p{color:#627a68;font-size:14px;line-height:1.6}</style></head>
<body><div class="card"><div class="icon">✅</div>
<h2>Successfully Unsubscribed</h2>
<p>You have been removed from this mailing list.</p>
</div></body></html>"""

# ── API routes ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/preview-template", methods=["POST"])
def preview_template():
    data = request.json
    try:
        return jsonify({"ok": True, "html": render_html(data.get("template",""), data.get("sample",{}))})
    except TemplateError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/parse-csv", methods=["POST"])
def parse_csv_route():
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "No file"}), 400
    try:
        rows, columns = parse_file(file)
        if not rows:
            return jsonify({"ok": False, "error": "File appears empty"}), 400
        return jsonify({"ok": True, "columns": columns, "count": len(rows), "sample": rows[:3], "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read file: {e}"}), 400

@app.route("/api/launch", methods=["POST"])
def launch():
    import json as _json
    # Multipart = has attachments; JSON = no attachments
    if request.content_type and "multipart" in request.content_type:
        data        = _json.loads(request.form.get("data", "{}"))
        attachments = [(f.filename, f.read()) for f in request.files.getlist("attachments") if f.filename]
    else:
        data        = request.json or {}
        attachments = []

    cfg          = data.get("smtp", {})
    recipients   = data.get("recipients", [])
    template_str = data.get("template", "")
    use_ai       = data.get("use_ai", False)
    dry_run      = data.get("dry_run", True)
    sched        = data.get("scheduled_at", None)

    if not recipients:   return jsonify({"ok": False, "error": "No recipients"}), 400
    if not template_str: return jsonify({"ok": False, "error": "No template"}), 400

    campaign_id = str(uuid.uuid4())[:8]
    state = {
        "status": "pending", "total": len(recipients),
        "sent": 0, "failed": 0, "skipped": 0,
        "opens": 0, "unsubscribes": 0,
        "current": 0, "current_email": "",
        "progress": 0, "log": [], "cancelled": False,
        "scheduled_for": sched,
        "created_at": datetime.utcnow().isoformat(),
        "attachments": [name for name, _ in attachments],
    }
    if attachments:
        state["log"].append({"type": "info", "msg": f"📎 {len(attachments)} attachment(s): {', '.join(n for n,_ in attachments)}"})

    c = load_campaigns()
    c[campaign_id] = state
    save_campaigns(c)
    campaigns[campaign_id] = state

    base_url = request.host_url.rstrip("/")

    if sched:
        try:
            fire_at = datetime.fromisoformat(sched.replace("Z", ""))
            t = threading.Thread(target=schedule_campaign,
                args=(campaign_id, fire_at, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments),
                daemon=True)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid schedule time"}), 400
    else:
        t = threading.Thread(target=run_campaign,
            args=(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url, attachments),
            daemon=True)
    t.start()
    return jsonify({"ok": True, "campaign_id": campaign_id})

@app.route("/api/status/<campaign_id>")
def status(campaign_id):
    # Always read from file so we get latest opens/unsubs even after restart
    c = load_campaigns()
    state = c.get(campaign_id)
    if not state:
        return jsonify({"ok": False, "error": "Not found"}), 404
    total = state["total"]
    done  = state.get("sent",0) + state.get("failed",0) + state.get("skipped",0)
    state["progress"] = int((done / total) * 100) if total else 0
    # Sync latest opens/unsubs from their own files
    opens  = load_opens()
    unsubs = load_unsubs()
    state["opens"]        = len(set(o["email"] for o in opens.get(campaign_id, [])))
    state["unsubscribes"] = len(unsubs.get(campaign_id, set()))
    return jsonify({"ok": True, **state})

@app.route("/api/cancel/<campaign_id>", methods=["POST"])
def cancel(campaign_id):
    c = load_campaigns()
    if campaign_id in c:
        c[campaign_id]["cancelled"] = True
        save_campaigns(c)
        campaigns[campaign_id] = c[campaign_id]
    return jsonify({"ok": True})

@app.route("/api/campaigns")
def list_campaigns():
    c = load_campaigns()
    opens  = load_opens()
    unsubs = load_unsubs()
    result = []
    for cid, state in c.items():
        result.append({
            "id": cid,
            "status": state.get("status"),
            "total": state.get("total", 0),
            "sent": state.get("sent", 0),
            "failed": state.get("failed", 0),
            "opens": len(set(o["email"] for o in opens.get(cid, []))),
            "unsubscribes": len(unsubs.get(cid, set())),
            "created_at": state.get("created_at", ""),
            "scheduled_for": state.get("scheduled_for", ""),
        })
    return jsonify({"ok": True, "campaigns": sorted(result, key=lambda x: x["created_at"], reverse=True)})

@app.route("/api/opens/<campaign_id>")
def get_opens(campaign_id):
    return jsonify({"ok": True, "opens": load_opens().get(campaign_id, [])})

@app.route("/api/unsubs/<campaign_id>")
def get_unsubs(campaign_id):
    return jsonify({"ok": True, "unsubs": list(load_unsubs().get(campaign_id, set()))})

if __name__ == "__main__":
    app.run(debug=True, port=5000)