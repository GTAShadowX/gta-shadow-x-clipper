
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh

ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

COPY . .

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}

