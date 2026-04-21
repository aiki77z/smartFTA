FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG INSTALL_MINERU=false
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=10
ARG PIP_RESUME_RETRIES=20
ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=${PIP_TIMEOUT}

RUN printf 'Acquire::Retries "10";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\n' > /etc/apt/apt.conf.d/80-retries \
    && sed -i "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip \
    && if [ -n "$PIP_TRUSTED_HOST" ]; then \
         pip install --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" --retries "$PIP_RETRIES" --resume-retries "$PIP_RESUME_RETRIES" -r requirements.txt; \
       else \
         pip install --index-url "$PIP_INDEX_URL" --retries "$PIP_RETRIES" --resume-retries "$PIP_RESUME_RETRIES" -r requirements.txt; \
       fi \
    && if [ "$INSTALL_MINERU" = "true" ]; then pip install "mineru[all]"; fi

COPY . .

EXPOSE 8010

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
