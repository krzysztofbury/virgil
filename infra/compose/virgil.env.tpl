# Rendered on the operator's machine by `scripts/deploy.py env` (op inject) and
# written to /etc/virgil/virgil.env with mode 0600. Secrets stay in 1Password,
# item virgil-production-env in the Virgil vault, one field per variable.
# Settings that are not secret are written here as plain values.
VIRGIL_BASE_URL=https://virgil.pl
VIRGIL_LANDING_PAGE=true
VIRGIL_REGISTRATION_OPEN=false
VIRGIL_CONTACT_EMAIL=contact@virgil.pl
VIRGIL_REQUIRE_TOTP=true
VIRGIL_API_SENSITIVE=true
VIRGIL_API_FINANCE=true
VIRGIL_ADMIN_EMAILS={{ op://Virgil/virgil-production-env/VIRGIL_ADMIN_EMAILS }}

VIRGIL_MAIL_BACKEND=smtp
VIRGIL_MAIL_FROM=Virgil <no-reply@virgil.pl>
VIRGIL_MAIL_REPLY_TO=contact@virgil.pl
VIRGIL_SMTP_HOST={{ op://Virgil/virgil-production-env/VIRGIL_SMTP_HOST }}
VIRGIL_SMTP_PORT={{ op://Virgil/virgil-production-env/VIRGIL_SMTP_PORT }}
VIRGIL_SMTP_USERNAME={{ op://Virgil/virgil-production-env/VIRGIL_SMTP_USERNAME }}
VIRGIL_SMTP_PASSWORD={{ op://Virgil/virgil-production-env/VIRGIL_SMTP_PASSWORD }}

VIRGIL_INTERNAL_LLM_MODEL=gemini/gemini-3.8-flash
VIRGIL_INTERNAL_LLM_KEY={{ op://Virgil/virgil-production-env/VIRGIL_INTERNAL_LLM_KEY }}

VIRGIL_ALERT_WEBHOOK_URL={{ op://Virgil/virgil-production-env/VIRGIL_ALERT_WEBHOOK_URL }}

VIRGIL_OFFSITE_ENDPOINT={{ op://Virgil/virgil-production-env/VIRGIL_OFFSITE_ENDPOINT }}
VIRGIL_OFFSITE_BUCKET={{ op://Virgil/virgil-production-env/VIRGIL_OFFSITE_BUCKET }}
VIRGIL_OFFSITE_ACCESS_KEY_ID={{ op://Virgil/virgil-production-env/VIRGIL_OFFSITE_ACCESS_KEY_ID }}
VIRGIL_OFFSITE_SECRET_ACCESS_KEY={{ op://Virgil/virgil-production-env/VIRGIL_OFFSITE_SECRET_ACCESS_KEY }}
VIRGIL_OFFSITE_RECIPIENT={{ op://Virgil/virgil-production-env/VIRGIL_OFFSITE_RECIPIENT }}
