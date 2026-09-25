FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libldap2-dev \
    libsasl2-dev \
    ldap-utils \
    gcc \
    libpq-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Production-сервер. Dev-сервер (runserver) запускается вручную при отладке.
# --no-control-socket: сокет управления gunicorn (появился в 25.1) по умолчанию
# создаётся в $XDG_RUNTIME_DIR или $HOME/.gunicorn/. В compose контейнер работает
# под user 1000:1000, которого нет в /etc/passwd, поэтому HOME оказывается «/» —
# и gunicorn пишет в лог «Permission denied: /.gunicorn». Управлять сервисом через
# gunicornc мы не собираемся, так что сокет просто не нужен.
CMD ["gunicorn", "core.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--no-control-socket"]
