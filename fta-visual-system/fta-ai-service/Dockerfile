FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=10
ARG PIP_RESUME_RETRIES=20

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=${PIP_TIMEOUT}

COPY requirements.txt .

RUN pip install --upgrade pip \
    && if [ -n "$PIP_TRUSTED_HOST" ]; then \
         pip install --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" --retries "$PIP_RETRIES" --resume-retries "$PIP_RESUME_RETRIES" -r requirements.txt; \
       else \
         pip install --index-url "$PIP_INDEX_URL" --retries "$PIP_RETRIES" --resume-retries "$PIP_RESUME_RETRIES" -r requirements.txt; \
       fi

COPY app ./app

EXPOSE 8020

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
