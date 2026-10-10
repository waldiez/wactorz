#!/bin/sh
# Write the configuration Alertmanager starts with, into the directory given.
#
# With ALERT_WEBHOOK_URL set, every alert is POSTed there as Alertmanager's own
# JSON, with ALERT_WEBHOOK_TOKEN as a bearer token when that is set too. Without
# it alerts are delivered nowhere: they show in Alertmanager's own page, and
# nothing leaves this machine until someone says where to.
#
# The address and the token go into files beside the configuration, which names
# them, so neither appears in the configuration Alertmanager shows on its status
# page, and neither has to survive being quoted into YAML.
#
# A file named alertmanager.yml beside this script is used as it is instead: for
# email, Slack, several receivers, or anything else a single webhook cannot say.
set -eu

out_dir="${1:-/run/alertmanager}"
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
own_config="${ALERTMANAGER_OWN_CONFIG:-${script_dir}/alertmanager.yml}"
config="${out_dir}/alertmanager.yml"

# Only this user reads what is written here.
umask 077
mkdir -p "$out_dir"

if [ -f "$own_config" ]; then
    cp "$own_config" "$config"
    echo "[alertmanager] Using ${own_config}; ALERT_WEBHOOK_URL is not read."
    exit 0
fi

cat > "$config" <<'EOF_ROUTE'
route:
  receiver: default
  # One notification per alert name, however many nodes or actors it covers.
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: default
EOF_ROUTE

if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
    echo "[alertmanager] ALERT_WEBHOOK_URL is not set: alerts are listed here and sent nowhere."
    exit 0
fi

printf '%s' "$ALERT_WEBHOOK_URL" > "${out_dir}/webhook_url"
cat >> "$config" <<EOF_WEBHOOK
    webhook_configs:
      - url_file: ${out_dir}/webhook_url
        send_resolved: true
        timeout: 10s
EOF_WEBHOOK

if [ -n "${ALERT_WEBHOOK_TOKEN:-}" ]; then
    printf '%s' "$ALERT_WEBHOOK_TOKEN" > "${out_dir}/webhook_token"
    cat >> "$config" <<EOF_TOKEN
        http_config:
          authorization:
            type: Bearer
            credentials_file: ${out_dir}/webhook_token
EOF_TOKEN
fi
echo "[alertmanager] Alerts are sent to the webhook in ALERT_WEBHOOK_URL."
