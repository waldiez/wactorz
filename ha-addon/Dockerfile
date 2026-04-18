ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache \
    curl \
    git \
    jq \
    gcc \
    musl-dev \
    linux-headers

# Copy the local project into the container
WORKDIR /app
COPY . .

# Install the local version of wactorz
RUN pip install --no-cache-dir ".[all]"

# Setup the entrypoint
COPY ha-addon/run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
