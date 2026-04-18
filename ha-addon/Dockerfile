ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies
# git: needed for pip install from git
# jq: needed for run.sh config parsing
# gcc/musl-dev/linux-headers: needed for psutil/aiohttp C extensions on alpine
RUN apk add --no-cache \
    curl \
    git \
    jq \
    gcc \
    musl-dev \
    linux-headers

# Install wactorz from source
RUN pip install --no-cache-dir "wactorz[all] @ git+https://github.com/waldiez/wactorz.git"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
