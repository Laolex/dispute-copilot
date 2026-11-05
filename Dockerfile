FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
COPY entrypoint.sh ./entrypoint.sh
RUN useradd --system --uid 10001 dc && mkdir -p /var/lib/dc && chown dc:dc /var/lib/dc \
    && chmod +x /srv/entrypoint.sh
ENV DC_DB=/var/lib/dc/dispute_copilot.db
EXPOSE 8000
# One worker: SQLite + in-process background tasks. Plenty for a sandbox demo.
# The entrypoint drops privileges; the app never runs as root. Listens on $PORT (Render sets it), default 8000.
ENTRYPOINT ["/srv/entrypoint.sh"]
