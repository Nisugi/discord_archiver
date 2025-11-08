FROM python:3.11-slim

RUN apt update && apt install -y procps && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archiver/ /app/archiver/
COPY .env* /app/
COPY . /app

RUN mkdir -p /data

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "archiver.main"]
