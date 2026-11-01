# The base images the add-ons are built on, pinned by digest so Dependabot keeps
# them current. Never built: the add-on image workflow reads the base for each
# add-on and architecture from the FROM line named `<add-on>-<arch>`. Each
# add-on's build.yaml and Dockerfile default name the floating tag of the same
# line, so a new dated build changes this file alone; a test keeps them on it.
FROM ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.24-2026.08.0@sha256:a0913e273a0ffbb00e58dcfcd8bca88634d3d00bb1d2de8407cdf93eef33a034 AS wactorz-aarch64
FROM ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.24-2026.08.0@sha256:2313257a84f90cbc94231d87e0aba100fee14e36ef4d4b0041d5d554b2ecd287 AS wactorz-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian:trixie-2026.10.1@sha256:a82c220d6389ecd1f65560f6794e889567a048efe82d121662f1a3d4ba0a3f95 AS wactorz-ultra-aarch64
FROM ghcr.io/home-assistant/amd64-base-debian:trixie-2026.10.1@sha256:cce8d6947a9f6389cbdc5bc6d1d43f72c1f6c47d7044376c5699f4185d9df280 AS wactorz-ultra-amd64
