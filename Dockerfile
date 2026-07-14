# HITL Document Review Workflow — HuggingFace Spaces Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System dependencies for PyMuPDF, plus curl for the HEALTHCHECK below.
# curl was invoked by the healthcheck but never installed, so the check could
# only ever fail and the container reported permanently unhealthy.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files
COPY . .

# Run unprivileged rather than as root. The app writes users.json, settings.json,
# .secret_key and the inbox/outbox at runtime, so /app must be writable by it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/docs_inbox /app/docs_outbox /app/extract_results \
    && chown -R appuser:appuser /app
USER appuser

# HF Spaces exposes port 7860. Note: web_app.py defaults to 8080 when run directly;
# gunicorn overrides that by binding to 7860 here. Run `python3 web_app.py` locally for 8080.
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:7860/health || exit 1

# gunicorn with gthread workers handles long-lived connections behind HF Spaces' proxy.
# No --preload so gunicorn binds port 7860 immediately and HF health check passes.
#
# --workers must stay at 1: review state falls back to an in-process dict when
# Supabase is not configured, and the login throttle and users/settings file lock
# are also per-process. Scaling out requires Supabase plus a shared store.
CMD ["gunicorn", \
     "--worker-class", "gthread", \
     "--workers", "1", \
     "--threads", "2", \
     "--timeout", "300", \
     "--bind", "0.0.0.0:7860", \
     "app:application"]
