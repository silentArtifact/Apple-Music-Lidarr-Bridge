FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/

ENV STATE_DIR=/data
VOLUME ["/data"]

# No args -> the polling loop. Pass --discover/--probe/--seed/--once for setup.
ENTRYPOINT ["python", "-u", "/app/bridge.py"]
