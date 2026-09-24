#!/bin/sh

# Установка процедуры автоочистки БД aisql (launchd-демоны, root).
# Канонический источник — этот каталог; файлы копируются в
# /usr/local/sbin и /Library/LaunchDaemons.
#
# Демоны:
#   com.tradesoft.corp-db-clean        — ежедневно 03:00 (StartCalendarInterval)
#   com.tradesoft.corp-db-clean-retry  — каждые 15 мин, повторно запускает
#                                        очистку, если ночной прогон не смог
#                                        подключиться к aisql
#
# Запускать: sudo ./install.sh  (обёртка ~/bin/sudo)

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

echo "==> Установка демона очистки БД aisql (corp-db-clean)..."
cp "$DIR/db-clean-aisql.sh" "$DB_CLEAN_SCRIPT"
chmod 755 "$DB_CLEAN_SCRIPT"
cp "$DIR/db-clean-aisql.py" "$DB_CLEAN_PY"
chmod 755 "$DB_CLEAN_PY"
cp "$DIR/com.tradesoft.corp-db-clean.plist" "$DB_CLEAN_PLIST"
: > "$DB_CLEAN_LOG" 2>/dev/null || true
if launchctl list | grep -q "$DB_CLEAN_LABEL"; then
  launchctl unload "$DB_CLEAN_PLIST" 2>/dev/null || true
fi
launchctl load -w "$DB_CLEAN_PLIST"

echo "==> Установка retry-демона (corp-db-clean-retry)..."
cp "$DIR/db-clean-retry.sh" "$DB_CLEAN_RETRY_SCRIPT"
chmod 755 "$DB_CLEAN_RETRY_SCRIPT"
cp "$DIR/com.tradesoft.corp-db-clean-retry.plist" "$DB_CLEAN_RETRY_PLIST"
if launchctl list | grep -q "$DB_CLEAN_RETRY_LABEL"; then
  launchctl bootout system "$DB_CLEAN_RETRY_PLIST" 2>/dev/null || true
fi
launchctl bootstrap system "$DB_CLEAN_RETRY_PLIST" 2>/dev/null || \
  launchctl load -w "$DB_CLEAN_RETRY_PLIST"

echo
echo "==> Проверка"
for l in "$DB_CLEAN_LABEL" "$DB_CLEAN_RETRY_LABEL"; do
  if launchctl list | grep -q "$l"; then
    echo "  [OK]   демон $l загружен"
  else
    echo "  [FAIL] демон $l не загружен"
  fi
done
echo "  [OK]   маркер retry: $( [ -f /var/run/corp-db-clean.retry ] && echo 'установлен (ждём чистки)' || echo 'отсутствует')"