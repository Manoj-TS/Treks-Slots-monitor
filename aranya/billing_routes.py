"""Checkout pages and the Razorpay webhook. The money logic lives in billing.py."""

from flask import Blueprint, g, jsonify, render_template, request

from . import accounts, billing, config, security, storage

bp = Blueprint("billing", __name__)

MAX_WEBHOOK_BYTES = 1_000_000


@bp.route("/billing")
def page():
    """Open to anyone, so a signed-out visitor following a link sees the price
    rather than an error. Buying needs a signed-in, confirmed account."""
    user = getattr(g, "user", None)
    online = billing.enabled()
    active = bool(user and user.has_access and not user.is_admin)
    return render_template(
        "billing.html", user=user, online=online,
        blocker=billing.purchase_blocker(user) if online else None,
        active=active,
        support_email=config.SUPPORT_EMAIL, price=config.PRICE_RUPEES,
        access_days=config.ACCESS_DAYS, csrf=security.csrf_token() if user else "")


@bp.route("/billing/order", methods=["POST"])
@security.verified_required
def order():
    if not billing.enabled():
        return jsonify({"error": "Online payment isn't available."}), 404
    try:
        return jsonify(billing.create_order(g.user))
    except billing.BillingError as e:
        return jsonify({"error": str(e)}), 400
    except billing.RazorpayError as e:
        print(f"[Billing] create order for user {g.user.id}: {e}")
        return jsonify({"error": "The payment provider isn't responding. "
                                 "Please try again in a minute."}), 502


@bp.route("/billing/verify", methods=["POST"])
@security.verified_required
def verify():
    """The browser's report of a successful checkout. Signed by Razorpay, but
    still only a claim: the payment itself is fetched and checked before any
    access is granted."""
    if not billing.enabled():
        return jsonify({"error": "Online payment isn't available."}), 404
    body = request.get_json(silent=True) or {}
    order_id = str(body.get("razorpay_order_id") or "")
    payment_id = str(body.get("razorpay_payment_id") or "")
    signature = str(body.get("razorpay_signature") or "")

    if not billing.verify_checkout_signature(order_id, payment_id, signature):
        return jsonify({"error": "That payment confirmation couldn't be verified."}), 400
    if billing.order_owner(order_id) != g.user.id:
        return jsonify({"error": "That order isn't on this account."}), 404

    pending = {"ok": False, "pending": True,
               "message": "Payment received — confirming it with the bank. Your access "
                          "will switch on within a few minutes; you'll get an email when "
                          "it does."}
    try:
        outcome, grant = billing.settle(order_id, billing.fetch_payment(payment_id), "checkout")
    except billing.RazorpayError as e:
        print(f"[Billing] verify {order_id}/{payment_id}: {e}")
        return jsonify(pending), 202

    if outcome in ("applied", "already"):
        storage.reload_user(g.user.id)
        security.evict_user(g.user.id)
        until = grant["until"] if grant else _current_until(g.user.id)
        return jsonify({"ok": True,
                        "until": until.strftime("%d %B %Y") if until else None})
    if outcome == "pending":
        return jsonify(pending), 202
    return jsonify({"ok": False,
                    "message": f"Something about this payment didn't match our records, so "
                               f"access wasn't switched on automatically. We've been "
                               f"notified — or write to {config.SUPPORT_EMAIL} quoting "
                               f"payment {payment_id}."}), 409


def _current_until(user_id: int):
    u = accounts.get_user(user_id)
    return u.access_until if u else None


@bp.route("/webhooks/razorpay", methods=["POST"])
def webhook():
    if not billing.enabled():
        return "", 404
    if (request.content_length or 0) > MAX_WEBHOOK_BYTES:
        return "", 413
    if not storage.db_ready():
        return "", 503                      # Razorpay retries
    # The signature is over the exact bytes sent; read them before anything
    # parses the body.
    body = request.get_data(cache=False)
    if len(body) > MAX_WEBHOOK_BYTES:
        return "", 413
    status = billing.handle_webhook(body, request.headers.get("X-Razorpay-Signature", ""),
                                    request.headers.get("X-Razorpay-Event-Id"))
    return "", status
