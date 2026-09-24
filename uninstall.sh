#!/bin/sh

# Удаление процедуры автоочистки БД aisql (launchd-демоны, root).
# Запускать: sudo ./uninstall.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

DB_CLEAN_LABEL="com.tradesoft.corp-db-clean"
DB_CLEAN_RETRY_LABEL="com.tradesoft.corp-db-clean-retry"
DB_CLEAN_PLIST="/Library/LaunchDaemons/$DB_CLEAN_LABEL.plist"
DB_CLEAN_RETRY_PLIST="/Library/LaunchDaemons/$DB_CLEAN_RETRY_LABEL.plist"
DB_CLEAN_SCRIPT="/usr/local/sbin/corp-db-clean.sh"
DB_CLEAN_RETRY_SCRIPT="/usr/local/sbin/db-clean-retry.sh"
DB_CLEAN_PY="/usr/local/sbin/db-clean-aisql.py"
DB_CLEAN_LOG="/var/log/corp-db-clean.log"

echo "==> Остановка и удаление retry-демона (corp-db-clean-retry)..."
if launchctl list | grep -q "$DB_CLEAN_RETRY_LABEL"; then
  launchctl bootout system "$DB_CLEAN_RETRY_PLIST" 2>/dev/null || \
    launchctl unload "$DB_CLEAN_RETRY_PLIST" 2>/dev/null || true
fi
rm -f "$DB_CLEAN_RETRY_PLIST" "$DB_CLEAN_RETRY_SCRIPT"

echo "==> Остановка и удаление демона очистки БД aisql (corp-db-clean)..."
if launchctl list | grep -q "$DB_CLEAN_LABEL"; then
  launchctl unload "$DB_CLEAN_PLIST" 2>/dev/null || true
fi
rm -f "$DB_CLEAN_PLIST" "$DB_CLEAN_SCRIPT" "$DB_CLEAN_PY"
rm -f "$DB_CLEAN_LOG" "${DB_CLEAN_LOG}.stdout.log" "${DB_CLEAN_LOG}.stderr.log"
rm -f "$DB_CLEAN_RETRY_LOG" "${DB_CLEAN_RETRY_LOG}.stdout.log" "${DB_CLEAN_RETRY_LOG}.stderr.log"
rm -f /var/run/corp-db-clean.retry

echo "==> Проверка"
fail=0
for l in "$DB_CLEAN_LABEL" "$DB_CLEAN_RETRY_LABEL"; do
  if launchctl list | grep -q "$l"; then
    echo "  [FAIL] демон $l всё ещё загружен"
    fail=1
  else
    echo "  [OK]   демон $l удалён"
  fi
done
[ -e "$DB_CLEAN_SCRIPT" ] || [ -e "$DB_CLEAN_PY" ] && {
  echo "  [FAIL] файлы демона остались на диске"; fail=1
} || echo "  [OK]   файлы демона удалены"
exit $fail