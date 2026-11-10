# The base images the add-ons are built on, pinned by digest so Dependabot keeps
# them current. Never built: the add-on image workflow reads the base for each
# add-on and architecture from the FROM line named `<add-on>-<arch>`. Each
# add-on's build.yaml and Dockerfile default name the floating tag of the same
# line, so a new dated build changes this file alone; a test keeps them on it.
FROM ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.24-2026.08.0@sha256:a0913e273a0ffbb00e58dcfcd8bca88634d3d00bb1d2de8407cdf93eef33a034 AS wactorz-aarch64
FROM ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.24-2026.08.0@sha256:2313257a84f90cbc94231d87e0aba100fee14e36ef4d4b0041d5d554b2ecd287 AS wactorz-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian:trixie-2026.11.0@sha256:3f64e58a4169792bdb28ae0a587cc26030a242e95ac3b76fe8f35a28fee7072d AS wactorz-ultra-aarch64
FROM ghcr.io/home-assistant/amd64-base-debian:trixie-2026.11.0@sha256:a2a09d3983b771953c53a3a8c871f2c1d6d28bcf0ebaab09bc0eb80ffa5f4041 AS wactorz-ultra-amd64
