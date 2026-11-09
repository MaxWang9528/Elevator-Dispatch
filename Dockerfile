FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
# No third-party dependencies: the simulator and server use only the standard library.
COPY src/ src/
COPY web/ web/

# Settings are saved in /app/data; mount a volume there so they survive container rebuilds.
RUN useradd --create-home --uid 10001 sim && mkdir -p /app/data && chown sim:sim /app/data
USER sim
VOLUME /app/data

EXPOSE 8000
CMD ["python", "-m", "elevator_sim.server", "--host", "0.0.0.0", "--port", "8000", "--settings", "/app/data/settings.json"]
