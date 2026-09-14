FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 watchdog \
    && useradd --uid 10001 --gid watchdog --no-create-home watchdog \
    && mkdir /data /logs && chown watchdog:watchdog /data /logs
COPY watchdog ./watchdog
USER 10001:10001
CMD ["python", "-m", "watchdog.main"]
