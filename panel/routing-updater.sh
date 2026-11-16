#!/usr/bin/env bash
# routing-updater: установка на машину с доступом к API PasarGuard (обычно сама панель).
#
#   routing-updater.sh install     скрипт, настройки (если их нет), systemd service + timer
#   routing-updater.sh check       PASS / WARN / FAIL: настройки, план без записи, раздача nginx, timer
#   routing-updater.sh uninstall   убрать скрипт и юниты (настройки, базы и backup остаются)
#
# Имена и каталоги можно переопределить переменными окружения:
#   RU_NAME      имя установки (бинарник /usr/local/sbin/$RU_NAME, юниты $RU_NAME.*)
#   RU_CONF_DIR  каталог настроек (env + template.json)
#   RU_INTERVAL  как часто запускать (OnUnitActiveSec), по умолчанию 1h
set -euo pipefail

NAME="${RU_NAME:-pasarguard-routing-updater}"
CONF_DIR="${RU_CONF_DIR:-/etc/$NAME}"
INTERVAL="${RU_INTERVAL:-1h}"
BIN="/usr/local/sbin/$NAME"
ENV_FILE="$CONF_DIR/routing-updater.env"
HERE="$(cd "$(dirname "$0")" && pwd)"

fail=0
pass() { echo "PASS  $*"; }
warn() { echo "WARN  $*"; }
bad()  { echo "FAIL  $*"; fail=1; }

install_() {
  [[ $EUID -eq 0 ]] || { echo "нужен root"; exit 1; }
  install -m 0755 "$HERE/routing_updater.py" "$BIN"
  install -d -m 0750 "$CONF_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0600 "$HERE/routing-updater.env.example" "$ENV_FILE"
    sed -i "s#^RU_TEMPLATE=.*#RU_TEMPLATE=$CONF_DIR/template.json#; s#^RU_OUTPUT_DIR=.*#RU_OUTPUT_DIR=/var/lib/$NAME#" "$ENV_FILE"
    echo "создан $ENV_FILE — впишите адрес панели, логин и RU_PUBLIC_BASE"
  fi
  [[ -f "$CONF_DIR/template.json" ]] || install -m 0644 "$HERE/template.json" "$CONF_DIR/template.json"

  cat > "/etc/systemd/system/$NAME.service" <<EOF
[Unit]
Description=Routing profile and geo databases for Happ/INCY (PasarGuard)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $BIN run
TimeoutStartSec=600
EOF
  cat > "/etc/systemd/system/$NAME.timer" <<EOF
[Unit]
Description=Run $NAME periodically

[Timer]
OnBootSec=5min
OnUnitActiveSec=$INTERVAL
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$NAME.timer"
  echo "готово. Первый запуск вручную: systemctl start $NAME.service; journalctl -u $NAME -n 50"
}

check_() {
  [[ -x "$BIN" ]] && pass "скрипт $BIN" || bad "нет $BIN (install)"
  [[ -f "$ENV_FILE" ]] && pass "настройки $ENV_FILE" || { bad "нет $ENV_FILE"; exit 1; }
  if [[ -x "$BIN" ]]; then
    if out=$(/usr/bin/python3 "$BIN" --env "$ENV_FILE" check 2>&1); then
      pass "план собран (панель и источники доступны)"; echo "$out" | sed 's/^/      /'
    else
      bad "check: $(echo "$out" | tail -1)"
    fi
  fi
  base=$(grep -oP '^RU_PUBLIC_BASE=\K.*' "$ENV_FILE" | tr -d '"'"'" || true)
  if [[ -n "$base" ]]; then
    for f in routing.json geosite.dat geoip.dat geosite.dat.sha256; do
      code=$(curl -s -o /dev/null -w '%{http_code}' "$base/$f" || true)
      [[ "$code" == 200 ]] && pass "раздаётся $base/$f" || warn "$base/$f -> HTTP $code (nginx? первый запуск?)"
    done
  else
    warn "RU_PUBLIC_BASE пуст: базы не зеркалятся, autorouting выключен"
  fi
  systemctl is-active --quiet "$NAME.timer" && pass "timer активен" || warn "timer $NAME.timer не активен"
  last=$(systemctl show "$NAME.service" -p Result --value 2>/dev/null || true)
  [[ -z "$last" || "$last" == success ]] && pass "последний запуск: ${last:-нет}" || bad "последний запуск: $last (journalctl -u $NAME)"
  exit $fail
}

uninstall_() {
  [[ $EUID -eq 0 ]] || { echo "нужен root"; exit 1; }
  systemctl disable --now "$NAME.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/$NAME.service" "/etc/systemd/system/$NAME.timer" "$BIN"
  systemctl daemon-reload
  echo "удалено. Настройки ($CONF_DIR), базы и backup (/var/lib/$NAME) оставлены;"
  echo "заголовок routing в правилах панели остался — он больше не обновляется."
}

case "${1:-}" in
  install) install_ ;;
  check) check_ ;;
  uninstall) uninstall_ ;;
  *) echo "usage: $0 install|check|uninstall"; exit 2 ;;
esac
