FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/

ENV STATE_DIR=/data
VOLUME ["/data"]
EXPOSE 8080

# Healthy when the web UI is up and the poll loop has completed a cycle
# recently (the /healthz handler enforces 2x POLL_INTERVAL). Disable by
# overriding HEALTHCHECK in compose if running with WEB_ENABLED=false.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5); \
    sys.exit(0 if r.status==200 else 1)" || exit 1

# No args -> the polling loop + web UI. Pass --discover/--probe/--seed/--once/
# --backfill for one-shot CLI use (those never start the web server). Goes
# through run.py so web.py shares the same bridge module instance as the loop.
ENTRYPOINT ["python", "-u", "/app/run.py"]
