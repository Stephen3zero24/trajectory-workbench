FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-demo.txt /app/requirements-demo.txt
RUN pip install -r /app/requirements-demo.txt

COPY . /app

RUN mkdir -p /etc/opensandbox /app/output

EXPOSE 3100 8080

CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "3100"]
