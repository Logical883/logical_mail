"""
Bulk Personalized Email Sender — Flask Backend
"""

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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import openpyxl
from flask import Flask, jsonify, render_template, request
from jinja2 import Template, TemplateError

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── In-memory campaign state ──────────────────────────────────────────────────
campaigns: dict[str, dict] = {}   # campaign_id → state dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_csv(file_content: str) -> tuple[list[dict], list[str]]:
    """Parse CSV content, handling BOM and common encoding issues."""
    # Strip BOM if present
    if file_content.startswith('\ufeff'):
        file_content = file_content[1:]
    reader = csv.DictReader(io.StringIO(file_content))
    rows = [{k.strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
    columns = [c.strip() for c in (reader.fieldnames or [])]
    return rows, columns


def parse_excel(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Parse Excel (.xlsx / .xls) file bytes into rows and columns."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = list(ws.iter_rows(values_only=True))
    if not rows_iter:
        return [], []
    # First row = headers
    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows_iter[0])]
    rows = []
    for row in rows_iter[1:]:
        record = {headers[i]: str(v).strip() if v is not None else "" for i, v in enumerate(row)}
        # Skip completely empty rows
        if any(v for v in record.values()):
            rows.append(record)
    wb.close()
    return rows, headers


def parse_file(file) -> tuple[list[dict], list[str]]:
    """Auto-detect file type and parse accordingly."""
    filename = file.filename.lower()
    file_bytes = file.read()

    if filename.endswith(('.xlsx', '.xls')):
        return parse_excel(file_bytes)
    else:
        # Try UTF-8 first, fallback to latin-1
        try:
            content = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            content = file_bytes.decode('latin-1')
        return parse_csv(content)


def render_html(template_str: str, variables: dict) -> str:
    return Template(template_str).render(**variables)


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def generate_ai_paragraph(recipient: dict, client: anthropic.Anthropic) -> str:
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


def build_mime(sender_email: str, sender_name: str, subject: str, to_email: str, html_body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(strip_tags(html_body), "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_smtp(cfg: dict, mime_msg: MIMEMultipart) -> None:
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.ehlo()
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.sendmail(cfg["smtp_user"], mime_msg["To"], mime_msg.as_string())


# ── Campaign runner (runs in background thread) ────────────────────────────────

def run_campaign(campaign_id: str, cfg: dict, recipients: list[dict],
                 template_str: str, use_ai: bool, dry_run: bool):
    state = campaigns[campaign_id]
    state["status"] = "running"
    ai_client = anthropic.Anthropic() if use_ai else None
    total = len(recipients)

    for i, recipient in enumerate(recipients):
        if state.get("cancelled"):
            state["status"] = "cancelled"
            break

        # Auto-detect email column (case-insensitive, common naming variants)
        email = ""
        for key in recipient:
            if key.lower().strip() in ("email", "e-mail", "emailaddress", "email_address", "mail"):
                email = recipient[key].strip()
                break
        # Fallback: scan all values for anything that looks like an email
        if not email:
            for val in recipient.values():
                if "@" in str(val) and "." in str(val):
                    email = str(val).strip()
                    break

        state["current"] = i + 1
        state["current_email"] = email

        if not email or "@" not in email:
            state["skipped"] += 1
            state["log"].append({"type": "warn", "msg": f"Row {i+1}: No valid email found. Columns detected: {list(recipient.keys())}"})
            continue

        try:
            # AI paragraph
            if use_ai and ai_client:
                recipient["ai_paragraph"] = generate_ai_paragraph(recipient, ai_client)

            # Render subject & body
            subject = render_html(cfg.get("subject_template", "Hello {{first_name}}"), recipient)
            html_body = render_html(template_str, recipient)

            mime_msg = build_mime(
                cfg["smtp_user"], cfg.get("from_name", ""),
                subject, email, html_body
            )

            if dry_run:
                # Save preview
                preview_dir = Path(f"previews/{campaign_id}")
                preview_dir.mkdir(parents=True, exist_ok=True)
                safe = email.replace("@", "_at_").replace(".", "_")
                (preview_dir / f"{safe}.html").write_text(html_body)
                state["log"].append({"type": "info", "msg": f"✓ Preview saved for {email}"})
            else:
                send_smtp(cfg, mime_msg)
                state["log"].append({"type": "success", "msg": f"✓ Sent to {email}"})

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

        delay = float(cfg.get("delay_seconds", 1.0))
        time.sleep(delay)

    if not state.get("cancelled"):
        state["status"] = "done"
    state["progress"] = 100


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/preview-template", methods=["POST"])
def preview_template():
    data = request.json
    template_str = data.get("template", "")
    sample = data.get("sample", {})
    try:
        html = render_html(template_str, sample)
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
            return jsonify({"ok": False, "error": "File appears to be empty or unreadable"}), 400
        return jsonify({"ok": True, "columns": columns, "count": len(rows),
                        "sample": rows[:3], "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read file: {str(e)}"}), 400


@app.route("/api/launch", methods=["POST"])
def launch():
    data = request.json
    cfg = data.get("smtp", {})
    recipients = data.get("recipients", [])
    template_str = data.get("template", "")
    use_ai = data.get("use_ai", False)
    dry_run = data.get("dry_run", True)

    if not recipients:
        return jsonify({"ok": False, "error": "No recipients"}), 400
    if not template_str:
        return jsonify({"ok": False, "error": "No template"}), 400

    campaign_id = str(uuid.uuid4())[:8]
    campaigns[campaign_id] = {
        "status": "pending",
        "total": len(recipients),
        "sent": 0, "failed": 0, "skipped": 0,
        "current": 0, "current_email": "",
        "progress": 0,
        "log": [],
        "cancelled": False,
    }

    thread = threading.Thread(
        target=run_campaign,
        args=(campaign_id, cfg, recipients, template_str, use_ai, dry_run),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "campaign_id": campaign_id})


@app.route("/api/status/<campaign_id>")
def status(campaign_id: str):
    state = campaigns.get(campaign_id)
    if not state:
        return jsonify({"ok": False, "error": "Not found"}), 404
    total = state["total"]
    done = state["sent"] + state["failed"] + state["skipped"]
    state["progress"] = int((done / total) * 100) if total else 0
    return jsonify({"ok": True, **state})


@app.route("/api/cancel/<campaign_id>", methods=["POST"])
def cancel(campaign_id: str):
    state = campaigns.get(campaign_id)
    if state:
        state["cancelled"] = True
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)