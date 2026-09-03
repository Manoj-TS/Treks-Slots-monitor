"""Transactional email over Zoho SMTP. Stdlib only — no extra dependency.

Never send inline in a request handler: the Zoho handshake plus send takes
1-3 seconds, and an SMTP timeout would 500 the signup request *after* the user
row was created, leaving an account that can never be verified. Everything goes
through a bounded queue drained by a daemon thread.
"""

import queue
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config

_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=500)
_worker_started = False
_lock = threading.Lock()


def _send_now(to: str, subject: str, text: str, html: str | None = None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    # From must match the authenticated mailbox or Zoho rejects it as relaying.
    msg["From"] = formataddr((config.MAIL_FROM_NAME, config.SMTP_USER))
    msg["To"] = to
    msg["Reply-To"] = config.SUPPORT_EMAIL
    msg["Message-ID"] = make_msgid(domain="aranyavihaara.org")
    # Always include a plaintext part; HTML-only transactional mail scores badly.
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def _drain() -> None:
    while True:
        to, subject, text, html = _queue.get()
        try:
            _send_now(to, subject, text, html)
            print(f"[Mail] sent {subject!r} to {to}")
        except Exception as e:
            # Log and drop: a failed verification mail is recoverable via the
            # "resend" button, and retrying blindly against Zoho's rate limits
            # would make things worse.
            print(f"[Mail] FAILED {subject!r} to {to}: {e.__class__.__name__}: {e}")
        finally:
            _queue.task_done()


def start_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_drain, daemon=True, name="mail").start()


def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Queue a message. Returns False if mail isn't configured or the queue is
    full — callers surface that as "couldn't send, try again" rather than 500."""
    if not config.mail_configured():
        print(f"[Mail] not configured — would have sent {subject!r} to {to}")
        return False
    start_worker()
    try:
        _queue.put_nowait((to, subject, text, html))
        return True
    except queue.Full:
        print(f"[Mail] queue full — dropped {subject!r} to {to}")
        return False


# ── Message templates ─────────────────────────────────────────────────────── #

def send_verification(to: str, link: str) -> bool:
    return send(
        to,
        "Confirm your Aranya email",
        f"Welcome to Aranya.\n\n"
        f"Confirm your email address to activate your account:\n{link}\n\n"
        f"This link expires in {config.VERIFY_TOKEN_HOURS} hours. "
        f"If you didn't sign up, you can ignore this message.\n\n"
        f"— Aranya (unofficial trek slot board)\n",
    )


def send_password_reset(to: str, link: str) -> bool:
    return send(
        to,
        "Reset your Aranya password",
        f"Someone asked to reset the password for this Aranya account.\n\n"
        f"Set a new password:\n{link}\n\n"
        f"This link expires in {config.RESET_TOKEN_MINUTES} minutes and can be "
        f"used once. If you didn't ask for this, ignore this message — your "
        f"password has not changed.\n\n"
        f"— Aranya (unofficial trek slot board)\n",
    )


def send_password_changed(to: str) -> bool:
    return send(
        to,
        "Your Aranya password was changed",
        f"The password for this Aranya account was just changed, and all other "
        f"sessions were signed out.\n\n"
        f"If this wasn't you, contact {config.SUPPORT_EMAIL} immediately.\n\n"
        f"— Aranya (unofficial trek slot board)\n",
    )


def send_invite(to: str, link: str, days: int) -> bool:
    """Sent when an operator hands someone access directly, rather than the
    person signing up themselves."""
    return send(
        to,
        "You have been given access to Aranya",
        f"You've been given {days} days of access to Aranya — a board showing "
        f"trek slot availability across the Karnataka forest department portal.\n\n"
        f"Set a password and sign in here:\n{link}\n\n"
        f"This link expires in {config.RESET_TOKEN_MINUTES} minutes. If it lapses, use "
        f"'Forgot your password' at {config.PUBLIC_BASE_URL}/forgot to get a new one.\n\n"
        f"Aranya is unofficial and not affiliated with the Karnataka Forest "
        f"Department. It shows availability only — treks are still booked on the "
        f"official portal.\n\n"
        f"— Aranya\n",
    )


def send_access_granted(to: str, days: int, until: str) -> bool:
    """Sent when an existing account is topped up."""
    return send(
        to,
        "Your Aranya access has been extended",
        f"{days} days of access have been added to your Aranya account.\n\n"
        f"Your access now runs until {until}.\n\n"
        f"Open your board: {config.PUBLIC_BASE_URL}/app\n\n"
        f"— Aranya\n",
    )


def send_account_exists(to: str) -> bool:
    """Sent instead of an error when someone tries to sign up with an address
    that already has an account — avoids leaking which emails are registered."""
    return send(
        to,
        "You already have an Aranya account",
        f"Someone tried to create an Aranya account with this email address, "
        f"but one already exists.\n\n"
        f"Sign in here: {config.PUBLIC_BASE_URL}/login\n"
        f"Forgot your password? {config.PUBLIC_BASE_URL}/forgot\n\n"
        f"If this wasn't you, no action is needed.\n\n"
        f"— Aranya (unofficial trek slot board)\n",
    )
