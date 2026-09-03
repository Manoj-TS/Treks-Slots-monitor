"""Public pages: the landing page and the policy pages Razorpay requires."""

from flask import Blueprint, g, render_template

from . import config

bp = Blueprint("public", __name__)

LEGAL_UPDATED = "4 September 2026"


def _ctx(**extra):
    base = {"price": config.PRICE_RUPEES,
            "access_days": config.ACCESS_DAYS,
            "support_email": config.SUPPORT_EMAIL,
            "business_name": config.BUSINESS_NAME,
            "business_phone": config.BUSINESS_PHONE,
            "business_address": config.BUSINESS_ADDRESS,
            "updated": LEGAL_UPDATED,
            "user": getattr(g, "user", None)}
    base.update(extra)
    return base


@bp.route("/")
def landing():
    return render_template("landing.html", **_ctx())


@bp.route("/terms")
def terms():
    return render_template("legal/terms.html", **_ctx(title="Terms of Service"))


@bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html", **_ctx(title="Privacy Policy"))


@bp.route("/refunds")
def refunds():
    return render_template("legal/refunds.html", **_ctx(title="Refund & Cancellation Policy"))


@bp.route("/delivery")
def delivery():
    return render_template("legal/delivery.html", **_ctx(title="Delivery Policy"))


@bp.route("/contact")
def contact():
    return render_template("legal/contact.html", **_ctx(title="Contact Us"))
