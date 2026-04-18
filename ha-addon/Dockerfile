ARG BUILD_FROM
FROM alpine

# Install system dependencies
RUN apk add --no-cache \
    curl \
    git \
    jq \
    gcc \
    musl-dev \
    linux-headers \
    python3-dev \
    libffi-dev \
    openssl-dev

WORKDIR /app

# Copy ONLY what is needed for the python package to install
COPY pyproject.toml .
COPY requirements.txt .
COPY wactorz/ wactorz/
COPY README.md .

# Install dependencies and the package
RUN pip install --no-cache-dir "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@dev"

# Setup the entrypoint
COPY ha-addon/run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
