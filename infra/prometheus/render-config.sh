#!/bin/sh
set -eu

out_file="${1:-/etc/prometheus/prometheus.yml}"
scrape_interval="${PROMETHEUS_SCRAPE_INTERVAL:-15s}"
python_target="${PROMETHEUS_PYTHON_TARGET:-}"
if [ -n "$python_target" ]; then
    python_target="${python_target}:${REST_EXTERNAL_PORT:-8000}"
else
    python_target="wactorz:8000"
fi
monitor_mosquitto="${PROMETHEUS_MONITOR_MOSQUITTO:-1}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
template_file="${PROMETHEUS_TEMPLATE_FILE:-${script_dir}/prometheus.yml}"

is_enabled() {
    value="$(printf '%s' "${1:-0}" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

sed \
  -e "s|__PROMETHEUS_SCRAPE_INTERVAL__|${scrape_interval}|g" \
  -e "s|__PROMETHEUS_PYTHON_TARGET__|${python_target}|g" \
  "$template_file" >"$out_file"

# The app gates every route but /health once API_KEY is set, and /metrics is one
# of them — so an unauthenticated scrape gets 401 and the target simply goes
# down, with nothing logged and nothing on the dashboard to say why. The app
# accepts `Authorization: Bearer` for exactly this reason.
#
# Appended rather than templated: wactorz-python is the last job in the
# template, so these lines land inside it. Prometheus redacts `credentials` from
# its own config API, so the key is not readable through the Prometheus UI.
if [ -n "${API_KEY:-}" ]; then
    # Single-quoted, with any single quote doubled — the YAML escape. Unquoted,
    # a key beginning `&` parses as an anchor and the credential silently becomes
    # null: the config loads, Prometheus starts, and the scrape is unauthenticated
    # with nothing to show for it. A key containing `*` or `: ` fails louder.
    api_key_yaml=$(printf '%s' "$API_KEY" | sed "s/'/''/g")
cat >>"$out_file" <<EOF
    authorization:
      type: Bearer
      credentials: '${api_key_yaml}'
EOF
fi

if is_enabled "$monitor_mosquitto"; then
cat >>"$out_file" <<'EOF'

  - job_name: mosquitto-blackbox
    metrics_path: /probe
    params:
      module: [tcp_connect]
    static_configs:
      - targets: ["mosquitto:1883"]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
EOF
fi
