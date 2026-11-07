FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKBENCH_COLLECT=0 \
    COLLECT_NEWS=0 \
    COLLECT_ONCHAIN=0 \
    REPORT_AUTO=0

WORKDIR /app
COPY pyproject.toml ./
COPY ontology/ ./ontology/
COPY workbench/ ./workbench/
RUN python -m pip install --no-cache-dir .

USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "workbench.cloud:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
