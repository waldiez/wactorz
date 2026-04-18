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

# Install dependencies and the package
RUN pip install --no-cache-dir "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@dev"

# Setup the entrypoint
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
