#!/usr/bin/env bash
#
# One-shot deploy/update for the Aranya dashboard on aranyavihaara.org.
# Run as root from the project directory on the VPS:
#
#     sudo ./start.sh
#
# Idempotent — safe to re-run for every update:
#     cd /opt/aranyavihaara && sudo git pull && sudo ./start.sh
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. root check
if [ "$(id -u)" -ne 0 ]; then
  die "Please run: sudo ./start.sh"
fi

# ---------------------------------------------------------------- 2. docker
if ! command -v docker >/dev/null 2>&1; then
  log "Docker not found — installing…"
  apt-get update -y
  apt-get install -y ca-certificates curl
  if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh; then
    sh /tmp/get-docker.sh
  else
    warn "get.docker.com failed — falling back to distro packages."
    apt-get install -y docker.io docker-compose-v2
  fi
  systemctl enable --now docker
fi

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  log "Installing the docker compose plugin…"
  apt-get update -y
  apt-get install -y docker-compose-plugin
  DC="docker compose"
fi
log "Using: $DC"

if ! command -v dig >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y dnsutils || warn "Could not install dnsutils — DNS preflight will use a weaker check."
fi

# ---------------------------------------------------------------- 3. .env
if [ ! -f .env ]; then
  if [ -t 0 ]; then
    log "No .env found — let's create one."
    read -rp "Domain [aranyavihaara.org]: " D; D="${D:-aranyavihaara.org}"
    read -rp "Email for Let's Encrypt notices: " E
    [ -n "$E" ] || die "An email address is required for certificate issuance."
    read -rp "Use Let's Encrypt staging (untrusted cert, for testing)? [y/N]: " S
    [ "${S,,}" = "y" ] && ST=1 || ST=0
    {
      echo "DOMAIN=$D"
      echo "EMAIL=$E"
      echo "STAGING=$ST"
    } > .env
    chmod 600 .env
  else
    die "No .env and no terminal to prompt on. Run: cp .env.example .env && edit it."
  fi
fi

set -a; source ./.env; set +a
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "DOMAIN '$DOMAIN' doesn't look like a hostname."
[[ "$EMAIL" =~ @ ]] || die "EMAIL '$EMAIL' doesn't look like an email address."
STAGING="${STAGING:-0}"
log "Domain: $DOMAIN   Email: $EMAIL   Staging: $STAGING"

# ---------------------------------------------------------------- 4. dirs + ownership
mkdir -p data certbot/conf certbot/www/.well-known/acme-challenge nginx/conf.d
# MUST match the Dockerfile's `USER 10001`. If this is wrong the app runs fine,
# serves the board, and silently discards every favourite on write — check with
# `docker compose logs app | grep '\[Storage\]'` after first run (must be empty).
chown -R 10001:10001 data
chmod 755 certbot/www

# ---------------------------------------------------------------- 5. firewall
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  log "Opening 80/443 in ufw…"
  ufw allow 22/tcp  >/dev/null
  ufw allow 80/tcp  >/dev/null
  ufw allow 443/tcp >/dev/null
fi

# ---------------------------------------------------------------- 6. certificates
CERT="certbot/conf/live/${DOMAIN}/fullchain.pem"

if [ -f "$CERT" ] && [ "${FORCE_CERT:-0}" != "1" ]; then
  log "Certificate already present — skipping issuance."
else
  if [ "${SKIP_DNS_CHECK:-0}" != "1" ]; then
    log "Checking DNS points at this server before touching Let's Encrypt…"
    MY_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
    [ -n "$MY_IP" ] || die "Could not determine this server's public IP. Set SKIP_DNS_CHECK=1 to bypass."
    for host in "$DOMAIN" "www.$DOMAIN"; do
      if command -v dig >/dev/null 2>&1; then
        GOT="$(dig +short A "$host" @1.1.1.1 | tail -n1)"
      else
        GOT="$(getent hosts "$host" | awk '{print $1}' | tail -n1)"
      fi
      [ "$GOT" = "$MY_IP" ] || die "$host resolves to '${GOT:-nothing}', not this server's IP ($MY_IP).
  Fix the DNS A record in Hostinger hPanel, wait for propagation, then re-run.
  (Override with SKIP_DNS_CHECK=1 if you're sure this is fine.)"
    done
    log "DNS OK: $DOMAIN and www.$DOMAIN both point at $MY_IP."
  fi

  log "Starting nginx in bootstrap mode (no TLS) to serve the ACME challenge…"
  cp nginx/bootstrap.conf nginx/conf.d/default.conf
  $DC up -d nginx

  log "Reachability preflight (free — spends no Let's Encrypt attempts)…"
  TOKEN="preflight-$$"
  echo ok > "certbot/www/.well-known/acme-challenge/$TOKEN"
  ok=1
  for host in "$DOMAIN" "www.$DOMAIN"; do
    if ! curl -fsS --max-time 10 "http://$host/.well-known/acme-challenge/$TOKEN" | grep -q ok; then
      warn "http://$host/.well-known/acme-challenge/ did not respond correctly."
      ok=0
    fi
  done
  rm -f "certbot/www/.well-known/acme-challenge/$TOKEN"
  [ "$ok" = "1" ] || die "ACME reachability check failed — port 80 isn't reaching this container from the internet.
  Check: Hostinger hPanel > VPS > Firewall allows TCP 80, and DNS has propagated."

  STAGING_ARGS=(--cert-name "$DOMAIN")
  if [ "$STAGING" = "1" ]; then
    # A separate --cert-name keeps a staging (untrusted) cert out of the
    # live/$DOMAIN path that the check above inspects — otherwise every future
    # run would see "certs exist" and skip issuing a real one.
    STAGING_ARGS=(--staging --cert-name "${DOMAIN}-staging")
    warn "STAGING=1 — the certificate will NOT be trusted by browsers."
  fi

  log "Requesting certificate from Let's Encrypt…"
  $DC run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" -d "www.$DOMAIN" \
    --email "$EMAIL" --agree-tos --no-eff-email \
    --non-interactive --keep-until-expiring \
    "${STAGING_ARGS[@]}"
fi

# ---------------------------------------------------------------- 7. production config + up
log "Installing production nginx config…"
sed "s/__DOMAIN__/${DOMAIN}/g" nginx/site.conf > nginx/conf.d/default.conf

log "Building and starting the stack…"
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo latest)"
$DC up -d --build

$DC exec -T nginx nginx -t
$DC exec -T nginx nginx -s reload

# ---------------------------------------------------------------- 8. health checks
log "Verifying…"

ok=1
for i in $(seq 1 20); do
  if $DC exec -T app python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5020/api/meta',timeout=4)" >/dev/null 2>&1; then
    echo "  app: OK"; ok=0; break
  fi
  sleep 3
done
[ "$ok" = "0" ] || warn "app did not answer /api/meta internally within 60s."

CURL_FLAGS=(-fsS)
[ "$STAGING" = "1" ] && CURL_FLAGS+=(-k)
if curl "${CURL_FLAGS[@]}" -o /dev/null --max-time 15 "https://${DOMAIN}/api/meta"; then
  echo "  https://${DOMAIN}: OK"
else
  warn "https://${DOMAIN}/api/meta did not respond."
fi

if timeout 15 curl -N -s "${CURL_FLAGS[@]}" "https://${DOMAIN}/api/stream" | head -c 200 | grep -q '^data:'; then
  echo "  SSE streaming: OK"
else
  warn "No SSE event within 15s — check proxy_buffering/gzip in nginx or --send-bytes in the Dockerfile CMD."
fi

if $DC logs app 2>&1 | grep -q '\[Storage\]'; then
  warn "App logged a [Storage] error — ./data is probably not owned by uid 10001. Run: chown -R 10001:10001 data && docker compose restart app"
fi

# ---------------------------------------------------------------- 9. done
echo "============================================================"
echo "  Aranya is live: https://${DOMAIN}"
echo "------------------------------------------------------------"
echo "  State        : ./data/*.json          (persists across rebuilds)"
echo "  Certificates : ./certbot/conf         (auto-renew every 12h)"
echo "  Logs         : $DC logs -f app|nginx|certbot"
echo "  Update       : git pull && sudo ./start.sh"
echo "  Restart app  : $DC restart app"
echo "  Stop         : $DC down               (data is preserved)"
echo "============================================================"
echo "  Reminder: Hostinger hPanel > VPS > Firewall must also allow TCP 80/443"
echo "  (separate from ufw)."
echo "============================================================"
