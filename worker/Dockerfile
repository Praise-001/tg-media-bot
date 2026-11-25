FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# YouTube hides its video links behind a small JavaScript puzzle; yt-dlp solves it with Deno.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh \
    && useradd -m bot \
    && mkdir /data \
    && chown -R bot /app /data

ENV STATS_DB_PATH=/data/stats.db

USER bot
ENTRYPOINT ["./entrypoint.sh"]
