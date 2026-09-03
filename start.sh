#!/usr/bin/env bash
#
# Deploy/update the Aranya dashboard on a VPS that ALREADY runs a host-level
# nginx serving other sites (with certbot-managed certs).
#
# The app runs in Docker bound to 127.0.0.1:5020; the host nginx reverse-proxies
# to it and terminates TLS — the same pattern as the other apps on this box.
# This script deliberately does NOT run its own nginx or certbot containers,
# which would fight the host for ports 80/443.
#
# Run as root from the project directory:
#     sudo ./start.sh
#
# Idempotent — this is also the update command:
#     git pull && sudo ./start.sh
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Please run: sudo ./start.sh"

# ---------------------------------------------------------------- prerequisites
command -v docker >/dev/null 2>&1 || die "Docker is not installed."
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "Neither 'docker compose' nor 'docker-compose' is available."
fi

command -v nginx >/dev/null 2>&1 || die "Host nginx not found. This script expects the shared host nginx setup."

# ---------------------------------------------------------------- .env
if [ ! -f .env ]; then
  if [ -t 0 ]; then
    read -rp "Domain [aranyavihaara.org]: " D; D="${D:-aranyavihaara.org}"
    read -rp "Email for Let's Encrypt notices: " E
    [ -n "$E" ] || die "An email address is required."
    printf 'DOMAIN=%s\nEMAIL=%s\n' "$D" "$E" > .env
    chmod 600 .env
  else
    die "No .env. Run: cp .env.example .env && edit it."
  fi
fi

set -a; source ./.env; set +a
DOMAIN="${DOMAIN:-aranyavihaara.org}"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "DOMAIN '$DOMAIN' doesn't look like a hostname."
[[ "${EMAIL:-}" =~ @ ]] || die "EMAIL '${EMAIL:-}' doesn't look like an email address."
APP_PORT="${APP_PORT:-5020}"
log "Domain: $DOMAIN   Port: 127.0.0.1:$APP_PORT"

# ---------------------------------------------------------------- state dir
mkdir -p data
# MUST match the Dockerfile's `USER 10001`. Get this wrong and the app still
# runs and serves the board, but silently discards every favourite you add
# (_save_json swallows the error). Verified at the end of this script.
chown -R 10001:10001 data

# ---------------------------------------------------------------- container
log "Building and starting the container…"
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo latest)"
$DC up -d --build

log "Waiting for the app to answer…"
up=0
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null --max-time 4 "http://127.0.0.1:${APP_PORT}/api/meta"; then up=1; break; fi
  sleep 3
done
if [ "$up" != "1" ]; then
  warn "App did not answer on 127.0.0.1:${APP_PORT} within 60s. Recent logs:"
  $DC logs --tail 40 app || true
  die "Aborting before touching nginx."
fi
log "App is up on 127.0.0.1:${APP_PORT}."

# ---------------------------------------------------------------- nginx zones
ZONES=/etc/nginx/conf.d/aranyavihaara-zones.conf
if [ ! -f "$ZONES" ]; then
  log "Installing rate-limit zones -> $ZONES"
  cp nginx/aranyavihaara-zones.conf "$ZONES"
fi

# ---------------------------------------------------------------- nginx vhost
AVAIL="/etc/nginx/sites-available/${DOMAIN}"
ENABLED="/etc/nginx/sites-enabled/${DOMAIN}"

if [ ! -f "$AVAIL" ]; then
  log "Installing vhost -> $AVAIL"
  sed "s/aranyavihaara\.org/${DOMAIN}/g" nginx/aranyavihaara.org.conf > "$AVAIL"
else
  # Never overwrite: certbot --nginx edits this file in place to add the TLS
  # server block and the port-80 redirect. Rewriting it would wipe that.
  log "Vhost already exists — leaving it alone (certbot manages it)."
fi
[ -L "$ENABLED" ] || ln -s "$AVAIL" "$ENABLED"

log "Testing nginx config…"
nginx -t
systemctl reload nginx

# ---------------------------------------------------------------- certificate
if [ -d "/etc/letsencrypt/live/${DOMAIN}" ] && [ "${FORCE_CERT:-0}" != "1" ]; then
  log "Certificate for ${DOMAIN} already exists — skipping issuance."
else
  command -v certbot >/dev/null 2>&1 || die "certbot not found on the host."
  log "Requesting certificate via the nginx plugin (same as your other sites)…"
  certbot --nginx \
    -d "$DOMAIN" -d "www.${DOMAIN}" \
    --email "$EMAIL" --agree-tos --no-eff-email \
    --non-interactive --redirect --keep-until-expiring
  systemctl reload nginx
fi

# ---------------------------------------------------------------- verify
log "Verifying…"

if curl -fsS -o /dev/null --max-time 15 "https://${DOMAIN}/api/meta"; then
  echo "  https://${DOMAIN} : OK"
else
  warn "https://${DOMAIN}/api/meta did not respond."
fi

if timeout 15 curl -N -s "https://${DOMAIN}/api/stream" | head -c 200 | grep -q '^data:'; then
  echo "  SSE streaming    : OK"
else
  warn "No SSE event within 15s — check proxy_buffering/gzip in $AVAIL."
fi

if $DC logs app 2>&1 | grep -q '\[Storage\]'; then
  warn "App logged a [Storage] error — ./data ownership is wrong."
  warn "Fix: chown -R 10001:10001 data && $DC restart app"
else
  echo "  State writes     : OK"
fi

echo "============================================================"
echo "  Aranya is live: https://${DOMAIN}"
echo "------------------------------------------------------------"
echo "  State       : ./data/*.json      (survives rebuilds)"
echo "  App logs    : $DC logs -f app"
echo "  nginx vhost : $AVAIL"
echo "  Update      : git pull && sudo ./start.sh"
echo "  Restart     : $DC restart app"
echo "  Stop        : $DC down           (data preserved)"
echo "============================================================"
