"""Google sign-in.

Authlib does the OIDC heavy lifting: state, nonce, the discovery document,
JWKS fetch and key rotation, and ID-token signature + claims validation
(iss/aud/exp/nonce). Those are the parts that are easy to get subtly wrong in a
way that still appears to work.

Account linking rules, and why:
  * Look up by the provider's immutable `sub`, never by email — an email
    address can be changed at the provider.
  * Link Google to an existing password account only when Google asserts
    email_verified. Otherwise anyone who can get an unverified token for an
    address could seize the account.
  * Pre-hijacking defence: if Google links to a local account that was never
    email-verified, that account may have been created by an attacker who is
    sitting on a known password. Clear the password so only Google (or a fresh
    reset, which proves mailbox control) can get in.
"""

from authlib.integrations.flask_client import OAuth

from . import accounts, config

_oauth = OAuth()
_google = None

DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


def init_app(app) -> bool:
    """Register the Google client. Returns False if it isn't configured, in
    which case the sign-in button is simply not offered."""
    global _google
    if not (config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET):
        return False
    _oauth.init_app(app)
    _google = _oauth.register(
        name="google",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url=DISCOVERY,
        # Only non-sensitive scopes, so Google requires no verification review.
        client_kwargs={"scope": "openid email profile"},
    )
    return True


def enabled() -> bool:
    return _google is not None


def client():
    return _google


def upsert_from_claims(claims: dict):
    """Map verified Google claims onto a local account.

    Returns (user, error_message). The caller has already had Authlib validate
    the ID token's signature and claims.
    """
    subject = claims.get("sub")
    email = (claims.get("email") or "").strip()
    email_ok = bool(claims.get("email_verified"))
    name = claims.get("name")

    if not subject:
        return None, "Google did not return an account identifier."
    if not email:
        return None, "Google did not share an email address."

    # 1. Known identity — just sign in.
    user = accounts.get_user_by_oauth("google", subject)
    if user:
        return user, None

    if not email_ok:
        return None, ("Your Google account's email address isn't verified, so "
                      "we can't use it to sign in.")

    # 2. Existing local account with that address — link them.
    user = accounts.get_user_by_email(email)
    if user:
        if not user.email_verified:
            # Pre-hijacking defence, see the module docstring.
            accounts.clear_password(user.id)
        accounts.link_oauth(user.id, "google", subject, email)
        accounts.mark_verified(user.id)
        return accounts.get_user(user.id), None

    # 3. Brand new account. Google has verified the address, so no email
    #    confirmation round-trip is needed.
    user = accounts.create_user(email, password=None, name=name, email_verified=True)
    accounts.link_oauth(user.id, "google", subject, email)
    return user, None
