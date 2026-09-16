"""Razorpay payments: create an order, confirm the payment, switch access on.

The rule everything here serves: money taken means access granted, exactly
once. Three independent paths can each confirm a payment, and any of them is
enough on its own:

  checkout    the browser posts Razorpay's signed result to /billing/verify
  webhook     Razorpay posts order.paid / payment.captured to /webhooks/razorpay
  reconciler  a background pass asks Razorpay about every recent unpaid order

The browser path is fast but dies with a closed tab; the webhook is reliable but
can be misconfigured; the reconciler covers both. They all end in settle() and
then apply_payment(), whose latch (`payments.applied_at IS NULL`, checked in the
same UPDATE that sets it) means racing paths grant once.

A signature is never taken as proof of payment on its own: the payment is
fetched from Razorpay and its order, amount and currency are checked against
our own ledger row before anything is granted. Amounts come from config, never
from the browser.

Razorpay is called with plain `requests`: four endpoints don't justify the SDK.
"""

import hashlib
import hmac
import json
import threading
import time
from collections import deque

import requests

from . import accounts, config, db, mail, security, state, storage

API = "https://api.razorpay.com/v1"
PRODUCT = "access_30d"

RECONCILE_EVERY = 300          # seconds between reconciler passes
RECONCILE_MIN_AGE = 120        # leave fresh orders to the checkout and webhook
ABANDON_AFTER_HOURS = 48


class BillingError(Exception):
    """Something the customer did or can fix. The message is shown to them."""


class RazorpayError(Exception):
    """Razorpay refused or couldn't be reached. Not shown verbatim."""

    def __init__(self, status, body):
        super().__init__(f"Razorpay HTTP {status}: {str(body)[:300]}")
        self.status, self.body = status, body


def enabled() -> bool:
    return config.billing_configured()


def amount_paise() -> int:
    return config.PRICE_RUPEES * 100


# ── Razorpay API ──────────────────────────────────────────────────────────── #

def _call(method: str, path: str, **kw) -> dict:
    try:
        r = requests.request(method, API + path, timeout=15,
                             auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET), **kw)
    except requests.RequestException as e:
        raise RazorpayError(None, f"{e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise RazorpayError(r.status_code, r.text)
    return r.json()


def fetch_payment(payment_id: str) -> dict:
    return _call("GET", f"/payments/{payment_id}")


def capture(payment_id: str, amount: int, currency: str) -> dict:
    return _call("POST", f"/payments/{payment_id}/capture",
                 json={"amount": amount, "currency": currency})


def order_payments(order_id: str) -> list[dict]:
    return _call("GET", f"/orders/{order_id}/payments").get("items", [])


# ── Signatures ────────────────────────────────────────────────────────────── #

def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not (order_id and payment_id and signature):
        return False
    expected = _hmac(config.RAZORPAY_KEY_SECRET, f"{order_id}|{payment_id}".encode())
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(_hmac(config.RAZORPAY_WEBHOOK_SECRET, body), signature)


# ── Orders ────────────────────────────────────────────────────────────────── #

_order_times: dict[int, deque] = {}
_order_lock = threading.Lock()


def purchase_blocker(user) -> str | None:
    """Why this user can't buy right now, in words for them, or None."""
    if user is None:
        return "Sign in to buy access."
    if not user.email_verified:
        return "Confirm your email address first."
    if user.status != "active":
        return "This account is disabled. Contact support."
    until = user.access_until
    now = accounts.utcnow()
    if until and until > now:
        headroom = (now.timestamp() + config.PAID_ACCESS_CAP_DAYS * 86400) - until.timestamp()
        if headroom < config.ACCESS_DAYS * 86400:
            return (f"Your access already runs until {until.strftime('%d %B %Y')}. "
                    f"Access can't be bought more than {config.PAID_ACCESS_CAP_DAYS} "
                    f"days ahead — come back closer to that date.")
    return None


def _rate_limited(user_id: int) -> bool:
    now = time.time()
    with _order_lock:
        q = _order_times.setdefault(user_id, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= config.MAX_ORDERS_PER_HOUR:
            return True
        q.append(now)
    return False


def create_order(user) -> dict:
    """Open a Razorpay order for one access period and record it."""
    blocker = purchase_blocker(user)
    if blocker:
        raise BillingError(blocker)
    if _rate_limited(user.id):
        raise BillingError("Too many payment attempts. Please wait a while and try again.")

    amount = amount_paise()
    receipt = f"u{user.id}-{int(time.time())}"
    order = _call("POST", "/orders", json={
        "amount": amount, "currency": "INR", "receipt": receipt,
        "notes": {"user_id": str(user.id), "product": PRODUCT, "email": user.email},
    })
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, product, razorpay_order_id, amount_paise,"
            " currency, days_granted, notes) VALUES (%s, %s, %s, %s, 'INR', %s, %s)",
            (user.id, PRODUCT, order["id"], amount, config.ACCESS_DAYS,
             json.dumps({"receipt": receipt, "source": "checkout"})))
    return {"order_id": order["id"], "key_id": config.RAZORPAY_KEY_ID,
            "amount": amount, "currency": "INR", "email": user.email,
            "name": user.name or "",
            "description": f"{config.ACCESS_DAYS} days of access"}


def order_owner(order_id: str) -> int | None:
    with db.connection() as conn:
        r = conn.execute("SELECT user_id FROM payments WHERE razorpay_order_id = %s",
                         (order_id,)).fetchone()
    return r[0] if r else None


# ── Settling ──────────────────────────────────────────────────────────────── #

def _row(order_id: str):
    with db.connection() as conn:
        return conn.execute(
            "SELECT id, user_id, amount_paise, currency, applied_at IS NOT NULL"
            " FROM payments WHERE razorpay_order_id = %s", (order_id,)).fetchone()


def _note(order_id: str, **fields) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE payments SET notes = notes || %s::jsonb, updated_at = now()"
                     " WHERE razorpay_order_id = %s", (json.dumps(fields), order_id))


def settle(order_id: str, payment: dict, source: str) -> tuple[str, dict | None]:
    """Given a payment entity from Razorpay (fetched by us, or inside a
    verified webhook), grant access if it genuinely pays this order.

    Returns (outcome, grant):
      applied   access was switched on just now; grant holds the details
      already   this order had already been applied
      pending   not captured yet (or capture failed) — nothing granted
      mismatch  the payment doesn't match our order — nothing granted, admins told
      unknown   not an order we created
    """
    row = _row(order_id)
    if row is None:
        return "unknown", None
    _, user_id, want_amount, want_currency, applied = row
    if applied:
        return "already", None

    problems = []
    if payment.get("order_id") != order_id:
        problems.append(f"payment is for order {payment.get('order_id')}")
    if payment.get("amount") != want_amount:
        problems.append(f"amount {payment.get('amount')} paise, expected {want_amount}")
    if payment.get("currency") != want_currency:
        problems.append(f"currency {payment.get('currency')}, expected {want_currency}")
    if problems:
        detail = "; ".join(problems)
        _note(order_id, mismatch=detail, mismatch_payment=payment.get("id"))
        _alert_admins(
            "[Aranya] Payment mismatch — access NOT granted",
            f"A payment did not match the order it claims to pay, so no access was "
            f"granted.\n\nOrder: {order_id}\nPayment: {payment.get('id')}\n"
            f"User id: {user_id}\nProblem: {detail}\nSeen via: {source}\n\n"
            f"Check it in the Razorpay dashboard; refund or grant by hand as appropriate.")
        return "mismatch", None

    status = payment.get("status")
    if status == "authorized":
        # Authorised but not captured is money held, not money received; if it
        # is never captured Razorpay hands it back. Capture it ourselves rather
        # than depend on the dashboard's auto-capture setting.
        try:
            payment = capture(payment["id"], want_amount, want_currency)
        except RazorpayError as e:
            # Most often another path captured it a moment ago.
            print(f"[Billing] capture {payment['id']}: {e}")
            try:
                payment = fetch_payment(payment["id"])
            except RazorpayError:
                return "pending", None
        status = payment.get("status")
    if status != "captured":
        return "pending", None

    grant = apply_payment(order_id, payment["id"], source)
    return ("applied", grant) if grant else ("already", None)


def apply_payment(order_id: str, payment_id: str, source: str) -> dict | None:
    """Mark the order paid and extend access, in one transaction. Returns the
    grant, or None if this order had already been applied (by any path)."""
    with db.connection() as conn:
        r = conn.execute("SELECT user_id FROM payments WHERE razorpay_order_id = %s",
                         (order_id,)).fetchone()
        if not r:
            return None
        user_id = r[0]
        # Lock the account first, so two paths applying different orders for the
        # same person stack correctly, and two applying the same order serialise
        # on the latch below.
        before = conn.execute("SELECT access_until FROM users WHERE id = %s FOR UPDATE",
                              (user_id,)).fetchone()[0]
        latched = conn.execute(
            "UPDATE payments SET applied_at = now(), status = 'paid',"
            "   razorpay_payment_id = %s, updated_at = now(),"
            "   notes = notes || jsonb_build_object('applied_by', %s::text)"
            " WHERE razorpay_order_id = %s AND applied_at IS NULL"
            " RETURNING id, days_granted, amount_paise",
            (payment_id, source, order_id)).fetchone()
        if not latched:
            return None
        pay_id, days, amount = latched
        after = conn.execute(
            f"UPDATE users SET access_until = {accounts.EXTEND_ACCESS_SQL}"
            " WHERE id = %s RETURNING access_until, email",
            (days, config.PAID_ACCESS_CAP_DAYS, user_id)).fetchone()
        conn.execute("UPDATE payments SET access_before = %s, access_after = %s WHERE id = %s",
                     (before, after[0], pay_id))

    until, email = after
    print(f"[Billing] paid: order {order_id} payment {payment_id} user {user_id} "
          f"via {source}; access until {until.isoformat()}")
    storage.reload_user(user_id)
    security.evict_user(user_id)
    state.mark_changed()
    mail.send_payment_receipt(email, amount / 100, payment_id, order_id, days,
                              until.strftime("%d %B %Y"))
    return {"user_id": user_id, "until": until, "days": days, "amount_paise": amount}


# ── Webhooks ──────────────────────────────────────────────────────────────── #

def handle_webhook(body: bytes, signature: str, event_id: str | None) -> int:
    """Process one webhook delivery. Returns the HTTP status to answer with:
    400 for a bad signature, 500 to make Razorpay retry, 200 otherwise."""
    if not verify_webhook_signature(body, signature):
        print("[Billing] webhook with a bad signature rejected")
        return 400
    try:
        event = json.loads(body)
    except ValueError:
        return 400
    etype = event.get("event") or "unknown"
    event_id = event_id or "sha256:" + hashlib.sha256(body).hexdigest()

    # Dedupe on events already *processed*, not merely seen: an event that
    # failed half-way must be processed again when Razorpay retries it.
    with db.connection() as conn:
        done = conn.execute(
            "INSERT INTO webhook_events (event_id, event_type, payload)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (event_id) DO UPDATE SET payload = EXCLUDED.payload"
            " RETURNING processed_at",
            (event_id, etype, body.decode("utf-8", "replace"))).fetchone()[0]
    if done:
        return 200

    try:
        _dispatch(etype, event.get("payload") or {})
    except Exception as e:
        print(f"[Billing] webhook {etype} {event_id} failed: {e.__class__.__name__}: {e}")
        with db.connection() as conn:
            conn.execute("UPDATE webhook_events SET error = %s WHERE event_id = %s",
                         (f"{e.__class__.__name__}: {e}"[:1000], event_id))
        return 500
    with db.connection() as conn:
        conn.execute("UPDATE webhook_events SET processed_at = now(), error = NULL"
                     " WHERE event_id = %s", (event_id,))
    return 200


def _entity(payload: dict, name: str) -> dict:
    return (payload.get(name) or {}).get("entity") or {}


def _dispatch(etype: str, payload: dict) -> None:
    if etype in ("order.paid", "payment.captured", "payment.authorized"):
        payment = _entity(payload, "payment")
        order_id = payment.get("order_id") or _entity(payload, "order").get("id")
        if not order_id or not payment:
            return
        outcome, _ = settle(order_id, payment, f"webhook:{etype}")
        if outcome == "unknown":
            print(f"[Billing] {etype} for an order we didn't create ({order_id}); ignored")
    elif etype == "payment.failed":
        payment = _entity(payload, "payment")
        if payment.get("order_id") and _row(payment["order_id"]):
            # A failed attempt isn't a failed order: the customer can retry on
            # the same order, so the status stays open.
            _note(payment["order_id"], last_failure=(
                payment.get("error_description") or payment.get("error_code") or "failed"))
    elif etype == "refund.processed":
        _record_refund(_entity(payload, "refund"))


def _record_refund(refund: dict) -> None:
    payment_id, refund_id = refund.get("payment_id"), refund.get("id")
    if not payment_id or not refund_id:
        return
    with db.connection() as conn:
        r = conn.execute(
            "SELECT p.id, p.amount_paise, p.notes, u.email, p.access_after"
            " FROM payments p JOIN users u ON u.id = p.user_id"
            " WHERE p.razorpay_payment_id = %s FOR UPDATE OF p", (payment_id,)).fetchone()
        if not r:
            print(f"[Billing] refund {refund_id} for unknown payment {payment_id}")
            return
        pid, amount, notes, email, access_after = r
        refunds = dict((notes or {}).get("refunds") or {})
        if refund_id in refunds:
            return
        refunds[refund_id] = int(refund.get("amount") or 0)
        total = sum(refunds.values())
        full = total >= amount
        conn.execute(
            "UPDATE payments SET notes = notes || %s::jsonb, updated_at = now(),"
            " status = CASE WHEN %s THEN 'refunded' ELSE status END WHERE id = %s",
            (json.dumps({"refunds": refunds, "refunded_paise": total}), full, pid))
    _alert_admins(
        f"[Aranya] Refund processed — {email}",
        f"Razorpay processed a refund of Rs {refunds[refund_id] / 100:.2f} for {email} "
        f"(payment {payment_id}, refund {refund_id}).\n"
        f"Refunded so far on this payment: Rs {total / 100:.2f} of Rs {amount / 100:.2f}"
        f"{' (full refund)' if full else ' (partial)'}.\n\n"
        f"Access was NOT changed automatically. That payment extended access to "
        f"{access_after.strftime('%d %b %Y') if access_after else 'unknown'}. "
        f"If access should end, use Revoke in {config.PUBLIC_BASE_URL}/admin.")


def _alert_admins(subject: str, text: str) -> None:
    try:
        recipients = accounts.admin_emails()
    except Exception as e:
        print(f"[Billing] could not load admin emails: {e}")
        recipients = []
    if config.ALERT_EMAIL and config.ALERT_EMAIL.lower() not in {r.lower() for r in recipients}:
        recipients.append(config.ALERT_EMAIL)
    print(f"[Billing] notice: {subject}")
    for to in recipients:
        mail.send(to, subject, text + "\n\n— Aranya billing\n")


# ── Reconciler ────────────────────────────────────────────────────────────── #

def reconcile_once() -> dict:
    """Ask Razorpay about recent unpaid orders; settle any that were paid."""
    counts = {"checked": 0, "applied": 0, "abandoned": 0}
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT razorpay_order_id FROM payments"
            " WHERE applied_at IS NULL AND status = 'created'"
            "   AND razorpay_order_id NOT LIKE 'manual\\_%%'"
            "   AND created_at < now() - make_interval(secs => %s)"
            "   AND created_at > now() - make_interval(hours => %s)"
            " ORDER BY created_at LIMIT 200",
            (RECONCILE_MIN_AGE, ABANDON_AFTER_HOURS)).fetchall()
    for (order_id,) in rows:
        counts["checked"] += 1
        try:
            items = order_payments(order_id)
        except RazorpayError as e:
            print(f"[Billing] reconcile {order_id}: {e}")
            continue
        # Prefer a captured payment; an authorised one gets captured by settle().
        items.sort(key=lambda p: {"captured": 0, "authorized": 1}.get(p.get("status"), 2))
        for p in items:
            if p.get("status") not in ("captured", "authorized"):
                continue
            outcome, _ = settle(order_id, p, "reconciler")
            if outcome == "applied":
                counts["applied"] += 1
            if outcome in ("applied", "already", "mismatch"):
                break
    with db.connection() as conn:
        cur = conn.execute(
            "UPDATE payments SET status = 'failed', updated_at = now(),"
            "   notes = notes || '{\"abandoned\": true}'::jsonb"
            " WHERE applied_at IS NULL AND status = 'created'"
            "   AND razorpay_order_id NOT LIKE 'manual\\_%%'"
            "   AND created_at <= now() - make_interval(hours => %s)",
            (ABANDON_AFTER_HOURS,))
        counts["abandoned"] = cur.rowcount
    if counts["applied"] or counts["abandoned"]:
        print(f"[Billing] reconciled: {counts}")
    return counts


def reconcile_loop() -> None:
    print("[Billing] reconciler started")
    while True:
        time.sleep(RECONCILE_EVERY)
        if not enabled() or not storage.db_ready():
            continue
        try:
            reconcile_once()
        except Exception as e:
            print(f"[Billing] reconcile pass failed: {e.__class__.__name__}: {e}")


_reconciler_started = False


def start_reconciler() -> None:
    global _reconciler_started
    if _reconciler_started or not enabled():
        return
    _reconciler_started = True
    threading.Thread(target=reconcile_loop, daemon=True, name="billing").start()


# ── Reading, for /admin ───────────────────────────────────────────────────── #

def recent_payments(limit: int = 20) -> list[dict]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT p.id, u.email, p.product, p.amount_paise, p.status,"
            " p.razorpay_order_id, p.razorpay_payment_id, p.created_at, p.applied_at,"
            " p.access_after, p.notes"
            " FROM payments p JOIN users u ON u.id = p.user_id"
            " ORDER BY p.created_at DESC, p.id DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        notes = r[10] or {}
        out.append({"id": r[0], "email": r[1], "product": r[2], "amount": r[3] / 100,
                    "status": r[4], "order_id": r[5], "payment_id": r[6],
                    "created": r[7], "applied": r[8], "until": r[9],
                    "manual": r[5].startswith("manual_"),
                    "refunded": (notes.get("refunded_paise") or 0) / 100,
                    "problem": notes.get("mismatch") or notes.get("last_failure")})
    return out
