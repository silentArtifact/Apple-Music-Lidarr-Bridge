FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/

ENV STATE_DIR=/data
VOLUME ["/data"]
EXPOSE 8080

# No args -> the polling loop + web UI. Pass --discover/--probe/--seed/--once/
# --backfill for one-shot CLI use (those never start the web server). Goes
# through run.py so web.py shares the same bridge module instance as the loop.
ENTRYPOINT ["python", "-u", "/app/run.py"]
