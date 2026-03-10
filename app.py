"""
LogiMail — Bulk Personalized Email Sender
Features: Open tracking, Unsubscribe handling, Scheduled sending
"""

import base64
import csv
import io
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
from flask import Flask, Response, jsonify, render_template, request
from jinja2 import Template, TemplateError

app = Flask(__name__)
app.secret_key = os.urandom(24)

campaigns: dict[str, dict] = {}
opens_db: dict[str, list] = {}
unsubs_db: dict[str, set] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

def parse_csv(file_content):
    if file_content.startswith('\ufeff'):
        file_content = file_content[1:]
    reader = csv.DictReader(io.StringIO(file_content))
    rows = [{k.strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
    columns = [c.strip() for c in (reader.fieldnames or [])]
    return rows, columns

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
    filename = file.filename.lower()
    file_bytes = file.read()
    if filename.endswith(('.xlsx', '.xls')):
        return parse_excel(file_bytes)
    try:
        content = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        content = file_bytes.decode('latin-1')
    return parse_csv(content)

def detect_email(recipient):
    for key in recipient:
        if key.lower().strip() in ("email", "e-mail", "emailaddress", "email_address", "mail"):
            return recipient[key].strip()
    for val in recipient.values():
        if "@" in str(val) and "." in str(val):
            return str(val).strip()
    return ""

def render_html(template_str, variables):
    return Template(template_str).render(**variables)

def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()

def generate_ai_paragraph(recipient, client):
    prompt = (
        f"Write a warm, 2-sentence personalised email opening for "
        f"{recipient.get('first_name', 'the recipient')} "
        f"who works at {recipient.get('company', 'their company')} "
        f"as a {recipient.get('role', 'professional')}. "
        "Be specific, human, and avoid filler phrases. No intro, just the paragraph."
    )
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

def inject_tracking(html_body, campaign_id, email):
    encoded_email = base64.urlsafe_b64encode(email.encode()).decode()
    pixel_url = f"/track/open/{campaign_id}/{encoded_email}"
    pixel_tag = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt=""/>'
    unsub_url = f"/track/unsub/{campaign_id}/{encoded_email}"
    html_body = html_body.replace("{{unsubscribe_link}}", unsub_url)
    html_body = html_body.replace("{{ unsubscribe_link }}", unsub_url)
    if "</body>" in html_body.lower():
        html_body = re.sub(r"</body>", f"{pixel_tag}</body>", html_body, flags=re.IGNORECASE)
    else:
        html_body += pixel_tag
    if "unsubscribe" not in html_body.lower():
        footer = f'<div style="text-align:center;padding:12px;font-family:sans-serif"><a href="{unsub_url}" style="color:#888;font-size:11px">Unsubscribe</a></div>'
        html_body = re.sub(r"</body>", f"{footer}</body>", html_body, flags=re.IGNORECASE)
    return html_body

def build_mime(sender_email, sender_name, subject, to_email, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(strip_tags(html_body), "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg

def send_smtp(cfg, mime_msg):
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.ehlo()
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.sendmail(cfg["smtp_user"], mime_msg["To"], mime_msg.as_string())

def run_campaign(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url=""):
    state = campaigns[campaign_id]
    state["status"] = "running"
    opens_db[campaign_id] = []
    unsubs_db.setdefault(campaign_id, set())
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = anthropic.Anthropic(api_key=api_key) if (use_ai and api_key) else None
    if use_ai and not api_key:
        state["log"].append({"type": "warn", "msg": "⚠️ AI personalization skipped — ANTHROPIC_API_KEY not set on server."})
        use_ai = False

    for i, recipient in enumerate(recipients):
        if state.get("cancelled"):
            state["status"] = "cancelled"
            break

        email = detect_email(recipient)
        state["current"] = i + 1
        state["current_email"] = email

        if not email or "@" not in email:
            state["skipped"] += 1
            state["log"].append({"type": "warn", "msg": f"Row {i+1}: No valid email found. Columns: {list(recipient.keys())}"})
            continue

        if email in unsubs_db.get(campaign_id, set()):
            state["skipped"] += 1
            state["log"].append({"type": "warn", "msg": f"Skipped unsubscribed: {email}"})
            continue

        try:
            if use_ai and ai_client:
                recipient["ai_paragraph"] = generate_ai_paragraph(recipient, ai_client)

            subject = render_html(cfg.get("subject_template", "Hello"), recipient)
            html_body = render_html(template_str, recipient)

            if not dry_run:
                html_body = inject_tracking(html_body, campaign_id, email)

            mime_msg = build_mime(cfg["smtp_user"], cfg.get("from_name", ""), subject, email, html_body)

            if dry_run:
                preview_dir = Path(f"previews/{campaign_id}")
                preview_dir.mkdir(parents=True, exist_ok=True)
                safe = email.replace("@", "_at_").replace(".", "_")
                (preview_dir / f"{safe}.html").write_text(html_body)
                state["log"].append({"type": "info", "msg": f"Preview saved for {email}"})
            else:
                send_smtp(cfg, mime_msg)
                state["log"].append({"type": "success", "msg": f"Sent to {email}"})

            state["sent"] += 1

        except TemplateError as e:
            state["failed"] += 1
            state["log"].append({"type": "error", "msg": f"Template error for {email}: {e}"})
        except smtplib.SMTPException as e:
            state["failed"] += 1
            state["log"].append({"type": "error", "msg": f"SMTP error for {email}: {e}"})
        except Exception as e:
            state["failed"] += 1
            state["log"].append({"type": "error", "msg": f"Error for {email}: {e}"})

        time.sleep(float(cfg.get("delay_seconds", 1.0)))

    if not state.get("cancelled"):
        state["status"] = "done"
    state["progress"] = 100

def schedule_campaign(campaign_id, fire_at, cfg, recipients, template_str, use_ai, dry_run, base_url):
    now = datetime.utcnow()
    delay = (fire_at - now).total_seconds()
    if delay > 0:
        campaigns[campaign_id]["status"] = "scheduled"
        campaigns[campaign_id]["log"].append({
            "type": "info",
            "msg": f"Scheduled for {fire_at.strftime('%Y-%m-%d %H:%M UTC')}"
        })
        time.sleep(delay)
    if not campaigns[campaign_id].get("cancelled"):
        run_campaign(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url)

@app.route("/track/open/<campaign_id>/<encoded_email>")
def track_open(campaign_id, encoded_email):
    try:
        email = base64.urlsafe_b64decode(encoded_email.encode()).decode()
        if campaign_id not in opens_db:
            opens_db[campaign_id] = []
        if not any(o["email"] == email for o in opens_db[campaign_id]):
            opens_db[campaign_id].append({"email": email, "time": datetime.utcnow().isoformat()})
            if campaign_id in campaigns:
                campaigns[campaign_id]["opens"] = len(opens_db[campaign_id])
                campaigns[campaign_id]["log"].append({"type": "info", "msg": f"👁 Opened by {email}"})
    except Exception:
        pass
    return Response(TRACKING_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control": "no-cache, no-store"})

@app.route("/track/unsub/<campaign_id>/<encoded_email>")
def track_unsub(campaign_id, encoded_email):
    try:
        email = base64.urlsafe_b64decode(encoded_email.encode()).decode()
        unsubs_db.setdefault(campaign_id, set()).add(email)
        if campaign_id in campaigns:
            campaigns[campaign_id]["unsubscribes"] = len(unsubs_db[campaign_id])
            campaigns[campaign_id]["log"].append({"type": "warn", "msg": f"Unsubscribed: {email}"})
    except Exception:
        pass
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"/>
    <style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
    min-height:100vh;margin:0;background:#0a0f0b;color:#e4ede6}
    .box{text-align:center;padding:48px 40px;background:#111a13;border-radius:12px;border:1px solid #243028}
    h2{color:#3ecf6e;margin:0 0 8px}p{color:#627a68;margin:0}</style></head>
    <body><div class="box"><h2>✓ Unsubscribed</h2>
    <p>You have been removed from this mailing list.</p></div></body></html>"""

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/preview-template", methods=["POST"])
def preview_template():
    data = request.json
    try:
        html = render_html(data.get("template", ""), data.get("sample", {}))
        return jsonify({"ok": True, "html": html})
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
        return jsonify({"ok": True, "columns": columns, "count": len(rows),
                        "sample": rows[:3], "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read file: {e}"}), 400

@app.route("/api/launch", methods=["POST"])
def launch():
    data = request.json
    cfg = data.get("smtp", {})
    recipients = data.get("recipients", [])
    template_str = data.get("template", "")
    use_ai = data.get("use_ai", False)
    dry_run = data.get("dry_run", True)
    scheduled_at = data.get("scheduled_at", None)

    if not recipients:
        return jsonify({"ok": False, "error": "No recipients"}), 400
    if not template_str:
        return jsonify({"ok": False, "error": "No template"}), 400

    campaign_id = str(uuid.uuid4())[:8]
    campaigns[campaign_id] = {
        "status": "pending",
        "total": len(recipients),
        "sent": 0, "failed": 0, "skipped": 0,
        "opens": 0, "unsubscribes": 0,
        "current": 0, "current_email": "",
        "progress": 0, "log": [],
        "cancelled": False,
        "scheduled_for": scheduled_at,
        "created_at": datetime.utcnow().isoformat(),
    }

    base_url = request.host_url.rstrip("/")

    if scheduled_at:
        try:
            fire_at = datetime.fromisoformat(scheduled_at.replace("Z", ""))
            thread = threading.Thread(
                target=schedule_campaign,
                args=(campaign_id, fire_at, cfg, recipients, template_str, use_ai, dry_run, base_url),
                daemon=True)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid schedule time"}), 400
    else:
        thread = threading.Thread(
            target=run_campaign,
            args=(campaign_id, cfg, recipients, template_str, use_ai, dry_run, base_url),
            daemon=True)

    thread.start()
    return jsonify({"ok": True, "campaign_id": campaign_id})

@app.route("/api/status/<campaign_id>")
def status(campaign_id):
    state = campaigns.get(campaign_id)
    if not state:
        return jsonify({"ok": False, "error": "Not found"}), 404
    total = state["total"]
    done = state["sent"] + state["failed"] + state["skipped"]
    state["progress"] = int((done / total) * 100) if total else 0
    return jsonify({"ok": True, **state})

@app.route("/api/cancel/<campaign_id>", methods=["POST"])
def cancel(campaign_id):
    state = campaigns.get(campaign_id)
    if state:
        state["cancelled"] = True
    return jsonify({"ok": True})

@app.route("/api/campaigns")
def list_campaigns():
    result = []
    for cid, state in campaigns.items():
        result.append({
            "id": cid,
            "status": state.get("status"),
            "total": state.get("total", 0),
            "sent": state.get("sent", 0),
            "failed": state.get("failed", 0),
            "opens": state.get("opens", 0),
            "unsubscribes": state.get("unsubscribes", 0),
            "created_at": state.get("created_at", ""),
            "scheduled_for": state.get("scheduled_for", ""),
        })
    return jsonify({"ok": True, "campaigns": sorted(result, key=lambda x: x["created_at"], reverse=True)})

@app.route("/api/opens/<campaign_id>")
def get_opens(campaign_id):
    return jsonify({"ok": True, "opens": opens_db.get(campaign_id, [])})

@app.route("/api/unsubs/<campaign_id>")
def get_unsubs(campaign_id):
    return jsonify({"ok": True, "unsubs": list(unsubs_db.get(campaign_id, set()))})

if __name__ == "__main__":
    app.run(debug=True, port=5000)