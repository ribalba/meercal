# meercal-server. The agent is not in here: it runs on your machine, next to
# the credentials, and this image has no code path that needs them.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the world.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY core /app/core
COPY app /app/app
COPY tools /app/tools
COPY VERSION /app/VERSION

# A non-root user, because nothing in here needs to be root and the image
# mounts a configuration file that holds nothing but is still not its business.
RUN useradd --create-home --uid 10001 meercal && chown -R meercal /app
USER meercal

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
