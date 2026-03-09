"""
Bulk Personalized Email Sender
- Reads recipients from CSV
- Uses Jinja2 templates for personalization
- Optionally uses Claude AI to generate unique content per recipient
- Sends via SMTP or saves previews to disk
"""

import csv
import smtplib
import logging
import time
import os
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional
from jinja2 import Template
import anthropic

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("email_campaign.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Config — edit or load from config.json
# ──────────────────────────────────────────────
class Config:
    # SMTP settings
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "your_email@gmail.com"
    SMTP_PASSWORD: str = "your_app_password"       # Use App Password for Gmail
    FROM_NAME: str = "Your Name"

    # Campaign settings
    SUBJECT_TEMPLATE: str = "Hi {{first_name}}, a note just for you"
    CSV_FILE: str = "recipients.csv"
    TEMPLATE_FILE: str = "email_template.html"
    OUTPUT_DIR: str = "previews"                   # Save previews here instead of sending
    DELAY_SECONDS: float = 1.0                     # Delay between sends to avoid throttling

    # AI personalisation (set to True to use Claude API)
    USE_AI_PERSONALIZATION: bool = False
    AI_FIELD_NAME: str = "ai_paragraph"            # Column name injected into template


def load_config(path: str = "config.json") -> Config:
    """Optionally load overrides from config.json."""
    cfg = Config()
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        log.info(f"Config loaded from {path}")
    return cfg


# ──────────────────────────────────────────────
# CSV helpers
# ──────────────────────────────────────────────
def load_recipients(csv_path: str) -> list[dict]:
    """Load recipients from a CSV file. Each row becomes a dict of template variables."""
    recipients = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Strip whitespace from all values
            recipients.append({k: v.strip() for k, v in row.items()})
    log.info(f"Loaded {len(recipients)} recipients from {csv_path}")
    return recipients


# ──────────────────────────────────────────────
# AI personalisation via Claude
# ──────────────────────────────────────────────
def generate_ai_paragraph(recipient: dict, client: anthropic.Anthropic) -> str:
    """Ask Claude to write a short personalised paragraph for this recipient."""
    prompt = (
        f"Write a warm, 2-sentence personalised email paragraph for {recipient.get('first_name', 'the recipient')} "
        f"who works at {recipient.get('company', 'their company')} as a {recipient.get('role', 'professional')}. "
        "Be specific, human, and helpful. No filler phrases."
    )
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ──────────────────────────────────────────────
# Template rendering
# ──────────────────────────────────────────────
def render_template(template_str: str, variables: dict) -> str:
    """Render a Jinja2 template string with the given variables."""
    tpl = Template(template_str)
    return tpl.render(**variables)


# ──────────────────────────────────────────────
# Email building
# ──────────────────────────────────────────────
def build_email(
    cfg: Config,
    recipient: dict,
    html_body: str,
) -> MIMEMultipart:
    subject = render_template(cfg.SUBJECT_TEMPLATE, recipient)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{cfg.FROM_NAME} <{cfg.SMTP_USER}>"
    msg["To"] = recipient["email"]

    # Plain-text fallback (strip tags naively)
    import re
    plain = re.sub(r"<[^>]+>", "", html_body).strip()
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


# ──────────────────────────────────────────────
# Sending / Preview
# ──────────────────────────────────────────────
def send_email(cfg: Config, msg: MIMEMultipart) -> bool:
    try:
        with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            server.sendmail(cfg.SMTP_USER, msg["To"], msg.as_string())
        return True
    except Exception as e:
        log.error(f"Failed to send to {msg['To']}: {e}")
        return False


def save_preview(cfg: Config, recipient: dict, msg: MIMEMultipart):
    """Save email as HTML file for review before sending."""
    Path(cfg.OUTPUT_DIR).mkdir(exist_ok=True)
    safe_name = recipient["email"].replace("@", "_at_").replace(".", "_")
    path = Path(cfg.OUTPUT_DIR) / f"{safe_name}.html"
    # Extract HTML part
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            path.write_text(part.get_payload(decode=True).decode("utf-8"))
            break
    log.info(f"  Preview saved → {path}")


# ──────────────────────────────────────────────
# Main campaign runner
# ──────────────────────────────────────────────
def run_campaign(
    cfg: Config,
    dry_run: bool = True,          # True = save previews only, False = actually send
    limit: Optional[int] = None,   # Send to only the first N recipients (for testing)
):
    # Load template
    tpl_path = Path(cfg.TEMPLATE_FILE)
    if not tpl_path.exists():
        log.error(f"Template file not found: {cfg.TEMPLATE_FILE}")
        return

    template_str = tpl_path.read_text(encoding="utf-8")

    # Load recipients
    recipients = load_recipients(cfg.CSV_FILE)
    if limit:
        recipients = recipients[:limit]

    # AI client (only created if needed)
    ai_client = anthropic.Anthropic() if cfg.USE_AI_PERSONALIZATION else None

    sent, failed, skipped = 0, 0, 0

    for i, recipient in enumerate(recipients, 1):
        email = recipient.get("email", "").strip()
        if not email or "@" not in email:
            log.warning(f"Row {i}: Invalid/missing email, skipping.")
            skipped += 1
            continue

        log.info(f"[{i}/{len(recipients)}] Processing {email}")

        # AI paragraph injection
        if cfg.USE_AI_PERSONALIZATION and ai_client:
            try:
                recipient[cfg.AI_FIELD_NAME] = generate_ai_paragraph(recipient, ai_client)
                log.info(f"  AI paragraph generated.")
            except Exception as e:
                log.warning(f"  AI generation failed: {e}. Using fallback.")
                recipient[cfg.AI_FIELD_NAME] = ""

        # Render HTML
        try:
            html_body = render_template(template_str, recipient)
        except Exception as e:
            log.error(f"  Template render failed: {e}")
            failed += 1
            continue

        # Build message
        msg = build_email(cfg, recipient, html_body)

        if dry_run:
            save_preview(cfg, recipient, msg)
            sent += 1
        else:
            if send_email(cfg, msg):
                log.info(f"  ✓ Sent to {email}")
                sent += 1
            else:
                failed += 1

        time.sleep(cfg.DELAY_SECONDS)

    log.info("─" * 50)
    log.info(f"Campaign complete. Sent: {sent} | Failed: {failed} | Skipped: {skipped}")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bulk Personalized Email Sender")
    parser.add_argument("--send", action="store_true", help="Actually send emails (default: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N recipients")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config JSON")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_campaign(cfg, dry_run=not args.send, limit=args.limit)