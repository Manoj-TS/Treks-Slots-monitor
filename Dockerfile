FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata \
    PYTHONPATH=/app \
    PORT=5020

# tzdata matters here: the board's rolling window is built from date.today().
# Under UTC the "next 30 days of weekends" would roll over 5.5h late every night.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --create-home --home-dir /home/app app

# Dependencies first so this layer stays cached across every code edit.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code last — a code-only change rebuilds in ~2s. Flask resolves aranya/templates
# and aranya/static relative to the package's location (not the CWD), so they
# work correctly even though the app runs with WORKDIR=/data below.
COPY wsgi.py /app/wsgi.py
COPY aranya/ /app/aranya/

RUN mkdir -p /data && chown -R 10001:10001 /data /app

USER 10001:10001

# The app writes favourites/watchlist/trek_configs/dashboard_settings as *relative*
# paths into the CWD. Running with WORKDIR=/data (a mounted volume) and the code on
# PYTHONPATH puts state on the volume with zero code changes. Do NOT mount at /app —
# that would shadow the aranya/ package.
WORKDIR /data

EXPOSE 5020

# /api/meta, not / — the latter returns ~50KB of inline HTML on every probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python","-c","import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5020/api/meta',timeout=4).status==200 else 1)"]

# waitress, deliberately — NOT gunicorn.
#   start_background() runs at import and spawns the portal-sweep threads. Gunicorn
#   forks workers and respawns them on timeout/max-requests; each respawn re-imports
#   the module and starts ANOTHER sweep loop hammering a government portal. waitress
#   is structurally one process, so that cannot happen.
#   --connection-limit must exceed --threads (default is 100 -> 502s at 100 viewers).
#   --send-bytes=1 forces a flush per yield so SSE events are not held back.
# Only flags verified working are listed here. The app doesn't read client IPs for
# anything (nginx handles rate limiting), so the --trusted-proxy* options are not
# needed; X-Forwarded-* headers still reach the app as ordinary headers.
CMD ["waitress-serve", \
     "--host=0.0.0.0", "--port=5020", \
     "--threads=200", "--connection-limit=400", "--channel-timeout=300", \
     "--send-bytes=1", \
     "wsgi:app"]
