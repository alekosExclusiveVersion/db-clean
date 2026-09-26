# Автоочистка БД aisql (db-clean)

Отдельный проект автоочистки БД `aisql.tradesoft.corp\supportsql` по закрытым
заявкам Битрикс24. Ранее жил в `~/Work/scripts/corp-vpn/`, выделен в
самостоятельный таск с собственной установкой.

## Как работает

- Имя БД вида `имя_клиента_<id1>[_<id2>...]` — каждая цифровая часть после
  подчёркивания считается номером сделки Б24 (например `client_123_456`).
- Сделки читаются из Б24 (`crm.deal.list/@ID` + `crm.deal.get`). БД удаляется
  **только когда ВСЕ указанные в имени сделки завершены**
  (`STAGE_SEMANTIC_ID='S'` и `CLOSED='Y'`).
- Режимы: `--dry-run` (по умолчанию, только кандидаты), `--commit` (удаляет).
- Результаты пишутся в `/var/log/corp-db-clean.log` и событиями в сигнальную
  очередь `/var/run/corp-vpn-signals` (`db-clean.ok/dryrun/fail`), откуда
  юзер-агент `corp-notify.sh` (в проекте corp-vpn) шлёт уведомления в
  macOS-баннер, B24-чат и Telegram-комнату (`~/Work/scripts/monitoring/notify.py`).

## Демоны (launchd, root)

| Label | Расписание | Назначение |
|---|---|---|
| `com.tradesoft.corp-db-clean` | ежедневно 03:00 | основной прогон |
| `com.tradesoft.corp-db-clean-retry` | каждые 15 мин | повторный запуск, пока стоит retry-маркер |

Retry-механика: если ночной прогон не смог подключиться к aisql или удаление
прошло с ошибками, ставится маркер `/var/run/corp-db-clean.retry` (первый раз).
Retry-демон каждые 15 минут пробует снова (через `db-clean-retry.sh` →
`corp-db-clean.sh --commit`); в тихом режиме (`DB_CLEAN_RETRY=1`) повторные
недоступности не дублируют сигнал `db-clean.fail`. Как только очистка прошла
успешно — маркер снимается, retry-демон останавливается (маркера нет).

На Windows автоматического retry нет: задача `tradesoft-db-clean` запускает
основной прогон ежедневно в 21:00. Переменная `DB_CLEAN_NOTIFY_ON_DELETE=1`
разрешает B24/Telegram-уведомления только при фактическом удалении БД; ошибки
и пустые результаты сохраняются в логе.

## Установка / удаление

```bash
~/bin/sudo ./install.sh      # копирует в /usr/local/sbin и /Library/LaunchDaemons
~/bin/sudo ./uninstall.sh    # удаляет демоны и файлы
```

`~/Work/scripts/corp-vpn/install.sh` и `uninstall.sh` вызывают эти скрипты
(через `DB_CLEAN_REPO`, по умолчанию `$HOME/Work/scripts/db-clean`).

## Секреты и доступ

Реальные пароли SQL — в `~/Library/Application Support/Parallels SQL Admin/
servers.json` (Fernet, ключ из Keychain), **не** в репо. Скрипт использует код
приложения `common.*` из `~/Work/scripts/parallels-sql-admins` (sys.path
настраивается сам). B24-вебхук — из `~/Work/ts-b24` (`b24_client.py`).

Тест доступности: `nc -z -G 5 aisql.tradesoft.corp 1433` (порт может быть
закрыт — named instance `supportsql` использует динамический порт через SQL
Browser 1434); реальная проверка — dry-run скрипта.

## Проверка

- `python3 -m py_compile db-clean-aisql.py`
- `DB_CLEAN_LOG=/tmp/db-clean.log python3 db-clean-aisql.py --dry-run`
  (показать кандидатов без удаления)
- Сигналы: `tail /var/run/corp-vpn-signals`