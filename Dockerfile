# The web app in hosted mode, for Cloud Run (P4). Kept slim: Artifact
# Registry's free tier is 0.5 GB, and P4 keeps 2 images.
#
#   docker build -t ph-statement-import .
#   docker run --rm -p 8080:8080 -e PUBLIC_URL=http://localhost:8080 \
#     -e APP_CONFIG='{"FLASK_SECRET_KEY":"local-test","YNAB_CLIENT_SECRET":"x"}' \
#     -e YNAB_CLIENT_ID=x ph-statement-import
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY statement_etl ./statement_etl
RUN useradd --uid 10001 app
USER app
# One worker: STORE lives in process memory. Threads handle concurrent requests.
# --preload imports the app before starting the worker, so a missing secret
# (config.py) stops the container instead of restarting the worker forever.
# --no-control-socket: gunicorn's admin socket isn't needed, and user app has no home.
CMD exec gunicorn -w 1 --threads 8 --timeout 60 --preload --no-control-socket \
    -b 0.0.0.0:${PORT:-8080} statement_etl.web.app:app
