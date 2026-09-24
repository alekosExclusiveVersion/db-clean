#!/bin/sh

# Повторный запуск автоочистки БД aisql, если ночной прогон (03:00) не смог
# подключиться к серверу. Маркер /var/run/corp-db-clean.retry ставит
# db-clean-aisql.py при недоступности aisql / ошибках удаления и снимает
# при успехе.
#
# Логика: пока маркер существует, каждые StartInterval сек (15 мин) пробуем
# снова. При успехе очистка снимет маркер, и демон перестанет запускаться
# (выход сразу, пока маркера нет). Retry-запуск идёт в тихом режиме
# (DB_CLEAN_RETRY=1): при недоступности БД не дублируется сигнал
# db-clean.fail (он уже отправлен ночным прогоном) — только лог.
#
# LaunchDaemon com.tradesoft.corp-db-clean-retry (StartInterval 900).

RETRY="/var/run/corp-db-clean.retry"
[ -f "$RETRY" ] || exit 0

DIR="$(cd "$(dirname "$0")" && pwd)"
DB_CLEAN_RETRY=1 exec "$DIR/corp-db-clean.sh" --commit