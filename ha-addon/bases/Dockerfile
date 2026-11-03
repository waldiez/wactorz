# The base images the add-ons are built on, pinned by digest so Dependabot keeps
# them current. Never built: the add-on image workflow reads the base for each
# add-on and architecture from the FROM line named `<add-on>-<arch>`. Each
# add-on's build.yaml and Dockerfile default name the floating tag of the same
# line, so a new dated build changes this file alone; a test keeps them on it.
FROM ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.24-2026.08.0@sha256:a0913e273a0ffbb00e58dcfcd8bca88634d3d00bb1d2de8407cdf93eef33a034 AS wactorz-aarch64
FROM ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.24-2026.08.0@sha256:2313257a84f90cbc94231d87e0aba100fee14e36ef4d4b0041d5d554b2ecd287 AS wactorz-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian:trixie-2026.10.2@sha256:7fb229440a6d85a848053394d58ab7fe28f12d759be2e039ba4ffbd41afdab33 AS wactorz-ultra-aarch64
FROM ghcr.io/home-assistant/amd64-base-debian:trixie-2026.10.2@sha256:ea3d6d850579853f776e7b2614a6c8c45ec4a61ad9e84f3271f91c41188d4ec4 AS wactorz-ultra-amd64
