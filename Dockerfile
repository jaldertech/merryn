FROM python:3.12-slim-bookworm

# libopus encodes the ballot hold music for voice playback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY merryn ./merryn
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

CMD ["merryn"]
