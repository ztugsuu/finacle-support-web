FROM python:3.12-slim AS oc-client
ARG OC_VERSION=4.14.27
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tar gzip && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux.tar.gz" -o /tmp/oc.tar.gz && \
    tar -xzf /tmp/oc.tar.gz -C /usr/local/bin oc && \
    chmod +x /usr/local/bin/oc && rm /tmp/oc.tar.gz

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=oc-client /usr/local/bin/oc /usr/local/bin/oc
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py restart_guard.py ./
COPY templates ./templates
COPY static ./static
USER 1001
EXPOSE 5000
# Process health only; cluster access is checked separately by the dashboard.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).close()"]
# Dry-run and apply can involve several sequential oc calls.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--timeout", "1800", "--graceful-timeout", "1800", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
