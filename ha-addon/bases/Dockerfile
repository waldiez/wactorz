# The base images the add-ons are built on, pinned by digest so Dependabot keeps
# them current. Never built: the add-on image workflow reads the base for each
# add-on and architecture from the FROM line named `<add-on>-<arch>`. Each
# add-on's build.yaml and Dockerfile default name the floating tag of the same
# line, so a new dated build changes this file alone; a test keeps them on it.
FROM ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.24-2026.08.0@sha256:a0913e273a0ffbb00e58dcfcd8bca88634d3d00bb1d2de8407cdf93eef33a034 AS wactorz-aarch64
FROM ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.24-2026.08.0@sha256:2313257a84f90cbc94231d87e0aba100fee14e36ef4d4b0041d5d554b2ecd287 AS wactorz-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian:trixie-2026.08.0@sha256:acc61c0ab60cbca176ec0fe4d37278be514a4a6babfc1becde951c99d17e92f0 AS wactorz-ultra-aarch64
FROM ghcr.io/home-assistant/amd64-base-debian:trixie-2026.08.0@sha256:9281ee991c28532ddae10114ac84750f4aca287a496f6e19583f3a750ad5e786 AS wactorz-ultra-amd64
