FROM python:3.11-slim

RUN useradd -m -u 10001 bot && mkdir /data && chown bot /data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot . .
USER bot

ENV DATA_DIR=/data
VOLUME /data

CMD ["python", "bot.py"]
