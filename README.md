# 📧 Bulk Personalized Email Sender

A Python system for sending personalized bulk emails with optional AI-generated content per recipient, powered by Claude.

---

## Project Structure

```
bulk_email_system/
├── email_sender.py       # Main script
├── email_template.html   # Jinja2 HTML email template
├── recipients.csv        # Your recipient list
├── config.json           # Settings (SMTP, AI, etc.)
├── requirements.txt      # Python dependencies
├── previews/             # Auto-created: HTML preview files (dry run)
└── email_campaign.log    # Auto-created: run log
```

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up your recipient CSV

Required column: `email`  
Recommended columns: `first_name`, `company`, `role`  
Add any custom columns — they're automatically available in the template.

```csv
email,first_name,company,role,pain_point,cta_link
alice@example.com,Alice,Acme,Marketing Lead,tracking ROI,https://yoursite.com/demo
```

### 3. Edit `config.json`

```json
{
  "SMTP_HOST": "smtp.gmail.com",
  "SMTP_PORT": 587,
  "SMTP_USER": "you@gmail.com",
  "SMTP_PASSWORD": "your_app_password",
  "FROM_NAME": "Your Name",
  "SUBJECT_TEMPLATE": "Hi {{first_name}}, quick note",
  "DELAY_SECONDS": 1.5
}
```

> **Gmail users**: Enable 2FA and use an [App Password](https://support.google.com/accounts/answer/185833).

### 4. Customize `email_template.html`

Template variables use `{{ variable_name }}` syntax (Jinja2).  
Any column from your CSV is automatically available.

### 5. Preview emails (dry run — no sending)
```bash
python email_sender.py
```
Opens HTML previews in the `previews/` folder.

### 6. Send to a test batch first
```bash
python email_sender.py --limit 3 --send
```

### 7. Send to everyone
```bash
python email_sender.py --send
```

---

## AI-Personalized Content (Claude)

Set `"USE_AI_PERSONALIZATION": true` in `config.json`.  
Make sure `ANTHROPIC_API_KEY` is in your environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Claude will generate a unique 2-sentence paragraph per recipient based on their `first_name`, `company`, and `role`. It's injected as `{{ ai_paragraph }}` in your template.

---

## CLI Reference

| Flag | Description |
|------|-------------|
| `--send` | Actually send emails (default is dry run) |
| `--limit N` | Only process the first N recipients |
| `--config PATH` | Use a custom config file path |

---

## Supported SMTP Providers

| Provider | Host | Port |
|----------|------|------|
| Gmail | smtp.gmail.com | 587 |
| Outlook | smtp.office365.com | 587 |
| Yahoo | smtp.mail.yahoo.com | 587 |
| SendGrid | smtp.sendgrid.net | 587 |
| Mailgun | smtp.mailgun.org | 587 |

For SendGrid/Mailgun via SMTP: use `apikey` as username and your API key as password.

---

## Tips

- Always do a **dry run** first and review the `previews/` folder
- Use `--limit 1 --send` to test with a single real send to yourself
- Add `DELAY_SECONDS` (1–2s) to avoid being flagged as spam
- Keep your CSV clean — invalid emails are auto-skipped and logged