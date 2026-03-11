# AgentFlow Rust server — multi-stage build
#
# Stage 1: builder  — compiles the binary with full toolchain
# Stage 2: runtime  — minimal Debian image, binary only

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM rust:1.93-bookworm AS builder

WORKDIR /build

# Cache dependency compilation separately from source compilation.
# Copy manifests first, build a dummy main, then copy real source.
COPY Cargo.toml Cargo.lock ./
COPY crates/agentflow-core/Cargo.toml        crates/agentflow-core/Cargo.toml
COPY crates/agentflow-mqtt/Cargo.toml        crates/agentflow-mqtt/Cargo.toml
COPY crates/agentflow-agents/Cargo.toml      crates/agentflow-agents/Cargo.toml
COPY crates/agentflow-interfaces/Cargo.toml  crates/agentflow-interfaces/Cargo.toml
COPY crates/agentflow-server/Cargo.toml      crates/agentflow-server/Cargo.toml

# Create stub lib/main files so Cargo can resolve the dep graph
RUN mkdir -p \
      crates/agentflow-core/src \
      crates/agentflow-mqtt/src \
      crates/agentflow-agents/src \
      crates/agentflow-interfaces/src \
      crates/agentflow-server/src && \
    echo 'pub fn _stub() {}' | tee \
      crates/agentflow-core/src/lib.rs \
      crates/agentflow-mqtt/src/lib.rs \
      crates/agentflow-agents/src/lib.rs \
      crates/agentflow-interfaces/src/lib.rs > /dev/null && \
    echo 'fn main() {}' > crates/agentflow-server/src/main.rs

RUN cargo build --release --bin agentflow 2>&1 | tail -5

# Now copy real source and rebuild (only changed crates recompile)
COPY crates/ crates/

# Touch all source files to force rebuild
RUN find crates -name '*.rs' -exec touch {} \;

RUN cargo build --release --bin agentflow

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime

# Install CA certs (needed for HTTPS LLM API calls) and curl (healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /build/target/release/agentflow /app/agentflow

# Non-root user
RUN useradd -r -u 1001 -g root agentflow && \
    chown agentflow /app/agentflow
USER agentflow

EXPOSE 8080 8081

ENTRYPOINT ["/app/agentflow"]
CMD ["--no-cli"]
