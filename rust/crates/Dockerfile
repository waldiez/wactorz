# wactorz Rust server — multi-stage build
#
# Stage 1: builder  — compiles the binary with full toolchain
# Stage 2: runtime  — minimal Debian image, binary only

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM rust:1.93-bookworm AS builder

WORKDIR /build

# Cache dependency compilation separately from source compilation.
# Copy manifests first, build a dummy main, then copy real source.
COPY Cargo.toml Cargo.lock ./
COPY crates/wactorz-core/Cargo.toml        crates/wactorz-core/Cargo.toml
COPY crates/wactorz-mqtt/Cargo.toml        crates/wactorz-mqtt/Cargo.toml
COPY crates/wactorz-agents/Cargo.toml      crates/wactorz-agents/Cargo.toml
COPY crates/wactorz-interfaces/Cargo.toml  crates/wactorz-interfaces/Cargo.toml
COPY crates/wactorz-server/Cargo.toml      crates/wactorz-server/Cargo.toml

# Create stub lib/main files so Cargo can resolve the dep graph
RUN mkdir -p \
      crates/wactorz-core/src \
      crates/wactorz-mqtt/src \
      crates/wactorz-agents/src \
      crates/wactorz-interfaces/src \
      crates/wactorz-server/src && \
    echo 'pub fn _stub() {}' | tee \
      crates/wactorz-core/src/lib.rs \
      crates/wactorz-mqtt/src/lib.rs \
      crates/wactorz-agents/src/lib.rs \
      crates/wactorz-interfaces/src/lib.rs > /dev/null && \
    echo 'fn main() {}' > crates/wactorz-server/src/main.rs

RUN cargo build --release --bin wactorz 2>&1 | tail -5

# Now copy real source and rebuild (only changed crates recompile)
COPY crates/ crates/

# Touch all source files to force rebuild
RUN find crates -name '*.rs' -exec touch {} \;

RUN cargo build --release --bin wactorz

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime

# Install CA certs (needed for HTTPS LLM API calls) and curl (healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/target/release/wactorz /app/wactorz

# Non-root user
RUN useradd -r -u 1001 -g root wactorz && \
    chown wactorz /app/wactorz
USER wactorz

EXPOSE 8080 8081

ENTRYPOINT ["/app/wactorz"]
CMD ["--no-cli"]
