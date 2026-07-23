FROM python:3.11.15-slim@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6 AS downward-builder

WORKDIR /build

RUN apt-get update && \
    apt-get install -y cmake g++ make && \
    rm -rf /var/lib/apt/lists/*

COPY lib/downward ./lib/downward
RUN python3 ./lib/downward/build.py


FROM python:3.11.15-slim@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6 AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=downward-builder /build/lib/downward/builds/release/bin \
    ./lib/downward/builds/release/bin

RUN groupadd --system planpilot && \
    useradd --system --gid planpilot --home-dir /app planpilot && \
    mkdir -p /app/instance /app/temp && \
    chown planpilot:planpilot /app/instance /app/temp

USER planpilot

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "330", "--no-control-socket", "run:app"]
