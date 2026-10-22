FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY 001_initial_schema.sql 002_sources_and_runs.sql 003_reliable_processing.sql 004_prefilter_and_signals.sql 005_decisions.sql 006_retention.sql ./
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
