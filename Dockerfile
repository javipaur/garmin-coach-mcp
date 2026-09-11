FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev libgeos-dev libproj-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY config.py date_utils.py localization.py auth.py recovery.py sleep.py activity.py coaching.py workout.py route_gen.py .
COPY server.py .
COPY index.html .

RUN mkdir -p /data/users

CMD ["python", "server.py"]
