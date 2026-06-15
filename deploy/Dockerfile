# swap — EMC cashier
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY swap ./swap
COPY scripts ./scripts

EXPOSE 8002
CMD ["uvicorn", "swap.main:app", "--host", "0.0.0.0", "--port", "8002"]
