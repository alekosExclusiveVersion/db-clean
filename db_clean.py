#!/usr/bin/env python3
"""Автоочистка БД aisql по стадиям заявок Б24.

Критерий удаления: имя БД вида имя_клиента_<id1>[_<id2>...] (каждая цифровая
часть после подчёркивания — номер сделки). БД удаляется только когда ВСЕ
указанные в имени сделки завершены: CLOSED='Y' и STAGE_SEMANTIC_ID в
TERMINAL_SEMANTICS (по умолчанию 'S' — успех и 'F' — провал/отказ; 'P' —
в работе). Набор терминальных семантик переопределяется env DB_CLEAN_TERMINAL
(через запятую) или флагом --only-success (только 'S').

При недоступности aisql или ошибках удаления ставится retry-маркер
(/var/run/corp-db-clean.retry); при успехе маркер снимается. Демон
corp-db-clean-retry каждые 15 мин повторяет очистку, пока маркер существует
(тихий режим DB_CLEAN_RETRY=1: повторные недоступности не дублируют сигнал).

Серверы: mssql-хост (--host, по умолчанию aisql) + все pgsql-серверы из
реестра PSA (движок pgsql; список переопределяется env
DB_CLEAN_PGSQL_HOSTS через запятую, формат host[:port]).

Режимы: --dry-run (по умолчанию) показывает кандидатов, --commit удаляет,
--sweep очищает файлы логов (журнал) у всех найденных БД без удаления,
--sweep-dry-run — только список БД для очистки (без выполнения).
Уведомления: в B24/Telegram уходит только фактическое удаление БД
(db-clean.ok с removed>0); FAIL/dryrun/нули — в лог и Windows-тост.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MONITORING_REPO = Path(os.environ.get(
    "MONITORING_REPO", "/Users/aleksei/Work/scripts/monitoring"))

def _default_psa_app() -> str:
    if os.name == "nt":
        return os.environ.get(
            "APPDATA", str(Path.home() / "AppData/Roaming")
        ) + "/Parallels SQL Admin"
    return "/Users/aleksei/Library/Application Support/Parallels SQL Admin"


PSA_ROOT = Path(os.environ.get("PSA_REPO", "/Users/aleksei/Work/scripts/parallels-sql-admins"))
B24_ROOT = Path(os.environ.get("B24_REPO", "/Users/aleksei/Work/ts-b24"))
PSA_APP = Path(os.environ.get("PSA_APP", _default_psa_app()))
AISQL_HOST = os.environ.get(
    "AISQL_HOST", r"aisql.tradesoft.corp\supportsql"
)
ENGINE_MSSQL = "mssql"
ENGINE_PGSQL = "pgsql"
SYSTEM_DBS = frozenset(
    ("master", "tempdb", "model", "msdb", "postgres", "template0", "template1",
     "information_schema", "performance_schema", "mysql", "sys")
)
RETRY_MARKER = Path(os.environ.get(
    "DB_CLEAN_RETRY_MARKER", "/var/run/corp-db-clean.retry"))
DEFAULT_TERMINAL_SEMANTICS = ("S", "F")
SEMANTIC_LABELS = {"S": "успех", "F": "провал", "P": "в работе"}
SUCCESS_ONLY_SEMANTICS = frozenset(("S",))
ROTATE_LINES = 500
BANNER_MAX_NAMES = 6


def terminal_semantics(only_success: bool = False) -> frozenset[str]:
    if only_success:
        return SUCCESS_ONLY_SEMANTICS
    raw = os.environ.get("DB_CLEAN_TERMINAL")
    if not raw:
        return frozenset(DEFAULT_TERMINAL_SEMANTICS)
    values = frozenset(v.strip().upper() for v in raw.split(",") if v.strip())
    return values or frozenset(DEFAULT_TERMINAL_SEMANTICS)


def deal_finished(deal, terminal: frozenset[str]) -> bool:
    if not deal or deal.get("CLOSED") != "Y":
        return False
    return deal.get("STAGE_SEMANTIC_ID") in terminal


def deal_state(deal) -> str:
    if not deal:
        return "нет сделки"
    semantic = deal.get("STAGE_SEMANTIC_ID")
    return SEMANTIC_LABELS.get(semantic, semantic or "?")


def deal_states(deals: dict, deal_ids: list[int]) -> str:
    return ", ".join(f"{i} ({deal_state(deals.get(i))})" for i in deal_ids)


def set_retry() -> None:
    try:
        if not RETRY_MARKER.exists():
            RETRY_MARKER.touch()
    except OSError:
        pass


def clear_retry() -> None:
    try:
        RETRY_MARKER.unlink()
    except OSError:
        pass


def log_line(path: Path, msg: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > ROTATE_LINES:
                path.write_text("\n".join(lines[-ROTATE_LINES // 2:]) + "\n",
                                encoding="utf-8")
    except OSError as ex:
        print(f"log error: {ex}", file=sys.stderr)


def signal(signals: Path, event: str, msg: str) -> None:
    try:
        signals.parent.mkdir(parents=True, exist_ok=True)
        with signals.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}|{event}|{msg}\n")
    except OSError as ex:
        print(f"signal error: {ex}", file=sys.stderr)


def notify_direct(event: str, msg: str) -> None:
    """Продакшн-уведомление в B24/Telegram напрямую (без corp-notify.sh).

    Только фактическое удаление БД. Остальное — в лог и Windows-тост.
    """
    try:
        sys.path.insert(0, str(MONITORING_REPO))
        from notify import notify_all

        title = "db-clean: " + event.removeprefix("db-clean.")
        notify_all(title, msg)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} notify {event}: {msg}")
    except Exception as ex:
        print(f"notify error {event}: {ex}", file=sys.stderr)


def notify_windows(event: str, msg: str) -> None:
    """Локальное уведомление Windows-тостом (без B24/Telegram)."""
    if os.name != "nt":
        return
    try:
        sys.path.insert(0, str(MONITORING_REPO))
        from notify import send_windows_toast

        title = "db-clean: " + event.removeprefix("db-clean.")
        send_windows_toast(title, msg)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} windows {event}: {msg}")
    except Exception as ex:
        print(f"windows notify error {event}: {ex}", file=sys.stderr)


def banner_list(names: list[str]) -> str:
    if len(names) <= BANNER_MAX_NAMES:
        return ", ".join(names)
    head = names[:BANNER_MAX_NAMES]
    return ", ".join(head) + f", и ещё {len(names) - BANNER_MAX_NAMES}"


def fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.1f} КБ"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.1f} МБ"
    return f"{num_bytes / 1024 ** 3:.1f} ГБ"


def ok_message(stats: dict[str, dict]) -> str:
    """Ok-уведомление: ✅, размер БД в скобках и список под каждым сервером."""
    lines = ["✅ Очистка БД выполнена"]
    for host, st in stats.items():
        if not st["removed"]:
            continue
        lines.append(f"{host}:")
        sizes = st.get("sizes") or {}
        shown = st["names"][:BANNER_MAX_NAMES]
        for name in shown:
            size = sizes.get(name)
            lines.append(f"• {name} ({size})" if size else f"• {name}")
        if len(st["names"]) > BANNER_MAX_NAMES:
            lines.append(f"• … и ещё {len(st['names']) - BANNER_MAX_NAMES}")
    return "\n".join(lines)


def one_line(value) -> str:
    return " ".join(str(value).split())


def precheck_network(host: str) -> str | None:
    """Быстрая предпроверка сети до SQL-хоста, без долгого SQL-таймаута.

    Возвращает None если сеть в порядке, иначе строку причины:
    DNS не резолвится (VPN/корп.DNS недоступен) либо нет ping
    (нет маршрута в корпсеть). Отключается env DB_CLEAN_SKIP_PRECHECK=1.
    """
    if os.environ.get("DB_CLEAN_SKIP_PRECHECK") == "1":
        return None
    server = host.split("\\")[0].split(",")[0].strip() or host
    if ":" in server:
        head, _, tail = server.rpartition(":")
        if tail.isdigit() and head:
            server = head
    try:
        ip = socket.gethostbyname(server)
    except socket.gaierror:
        return f"DNS {server} не резолвится (VPN/корп.DNS недоступен)"
    except Exception as ex:
        return f"DNS {server}: {one_line(ex)}"
    ping = shutil.which("ping")
    if not ping:
        return None
    try:
        if os.name == "nt":
            cmd = [ping, "-n", "1", "-w", "1000", server]
        else:
            cmd = [ping, "-c", "1", "-W", "1", server]
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0:
            return f"нет ping до {server} ({ip}) — нет маршрута/VPN down"
    except Exception as ex:
        return f"ping {server} ({ip}): {one_line(ex)}"
    return None


def load_psa() -> None:
    sys.path.insert(0, str(PSA_ROOT))
    import common.config as C

    cfg = C.load_config(PSA_APP / "config.ini")
    object.__setattr__(cfg.advanced, "servers_file", str(PSA_APP / "servers.json"))
    log_root = Path(os.environ.get("DB_CLEAN_LOG", "/var/log/corp-db-clean.log")).parent
    object.__setattr__(cfg.logging, "directory", log_root / "corp-db-clean")
    C.config = cfg

    from common.server_registry import registry

    registry.ensure_key()


def get_mssql():
    from common.mssql_client import mssql

    return mssql


def get_pgsql():
    from common.pgsql_client import pgsql

    return pgsql


def parse_host_list(raw: str | None) -> list[str] | None:
    """Разбор списка хостов host[:port] через запятую.

    Возвращает None когда значение env не задано (разрешить обнаружение
    по реестру), [] когда очистку pgsql нужно пропустить (пусто/off/none).
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw.lower() in ("off", "none", "0"):
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def list_pgsql_host_keys() -> list[str]:
    """Pgsql-хосты реестра PSA (engine=pgsql) в формате host[:port]."""
    from common.server_registry import registry

    out: list[str] = []
    for spec in registry.load():
        if spec.engine != ENGINE_PGSQL:
            continue
        host_key = spec.host_key()
        if host_key not in out:
            out.append(host_key)
    return out


def pgsql_targets() -> list[str]:
    """Список pgsql-хостов для очистки.

    Приоритет: DB_CLEAN_SKIP_PGSQL=1 → пропустить; DB_CLEAN_PGSQL_HOSTS
    (список через запятую, off/none = пропустить) → использовать его;
    иначе — все pgsql-серверы реестра PSA.
    """
    if os.environ.get("DB_CLEAN_SKIP_PGSQL") == "1":
        return []
    configured = parse_host_list(os.environ.get("DB_CLEAN_PGSQL_HOSTS"))
    if configured is not None:
        return configured
    return list_pgsql_host_keys()


def drop_database(client, engine: str, host: str, name: str) -> None:
    """DROP DATABASE: pgsql требует autocommit (нет transaction block)."""
    if engine != ENGINE_PGSQL:
        client.drop_database(host, name)
        return

    import psycopg

    from common.server_registry import registry

    user, password, real_host, port = registry.credentials_for(host)
    conn = psycopg.connect(
        host=real_host, port=port, user=user, password=password,
        dbname="postgres", autocommit=True,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pid FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            for row in cur.fetchall() or []:
                pid = row.get("pid") if isinstance(row, dict) else row[0]
                if pid:
                    try:
                        cur.execute(
                            "SELECT pg_terminate_backend(%s)", (int(pid),)
                        )
                    except Exception:
                        pass
            cur.execute(f'DROP DATABASE "{name}"')
    finally:
        conn.close()


def clean_database_logs(client, engine: str, host: str, name: str) -> bool:
    """Очищает файлы логов (журнал транзакций/данных) БД.

    - mssql: DBCC SHRINKFILE — RECOVERY SIMPLE → SHRINKFILE(2,0) → RECOVERY FULL.
    - pgsql: VACUUM FULL — пересборка физических файлов данных (autocommit).

    Возвращает True, если очистка завершилась успешно (иначе исключение).
    Вызывается sweep'ом; перед удалением (DROP удаляет файлы целиком) не нужна.
    """
    if engine == ENGINE_PGSQL:
        client.vacuum_full(host, name)
        return True
    client.shrink_log(host, name)
    return True


def engine_list_sql(engine: str) -> str:
    if engine == ENGINE_PGSQL:
        return "SELECT datname AS name FROM pg_database"
    return "SELECT name FROM sys.databases"


def list_databases(client, engine: str, host: str) -> list[str]:
    rows = client.query(host, engine_list_sql(engine))
    return sorted(r.get("name") for r in rows if r.get("name"))


def plan_sweep(client, engine: str, host: str) -> list[tuple[str, str]]:
    """Планирует очистку: [(name, size_str)] для всех нематериальных БД.

    Размер — до очистки, в человекочитаемом виде; системные БД пропускаются.
    """
    databases = list_databases(client, engine, host)
    sizes = client.database_sizes(host) or {}
    return [
        (n, fmt_size(int(sizes[n] or 0)) if n in sizes else "")
        for n in databases
        if n not in SYSTEM_DBS
    ]


def sweep_logs(client, engine: str, host: str, log: Path) -> list[tuple[str, str]]:
    """Очищает файлы логов (журнал) у всех найденных БД сервера (без удаления).

    Возвращает [(name, size_str)] очищенных БД (размер — до очистки, в
    человекочитаемом виде); системные БД пропускаются.
    """
    cleaned: list[tuple[str, str]] = []
    for name, size_str in plan_sweep(client, engine, host):
        clean_database_logs(client, engine, host, name)
        cleaned.append((name, size_str))
        log_line(log, f"SWEEP_LOGS host={host} {name} ok")
    return cleaned


def sweep_message(cleaned: dict[str, list[tuple[str, str]]]) -> str:
    """Сообщение об уведомлении по результату sweep: очищенные БД + объёмы."""
    lines = ["🧹 Очистка файлов логов завершена"]
    for host, entries in sorted(cleaned.items()):
        lines.append(f"{host}:")
        shown = entries[:BANNER_MAX_NAMES]
        for name, size in shown:
            lines.append(f"• {name} ({size})" if size else f"• {name}")
        if len(entries) > BANNER_MAX_NAMES:
            lines.append(f"• … и ещё {len(entries) - BANNER_MAX_NAMES}")
    return "\n".join(lines)


def parse_candidate(name: str) -> list[int] | None:
    """Возвращает список ID сделок для БД вида имя_клиента_<id1>[_<id2>...].

    По контракту каждая цифровая часть после подчёркивания — номер сделки.
    Если в имени несколько номеров (client_123_456), вернуть их все — процедура
    удалит БД только когда ВСЕ эти сделки завершены.
    """
    parts = name.split("_")
    if len(parts) < 2 or not parts[-1].isdigit():
        return None
    ids = [int(p) for p in parts if p.isdigit()]
    return ids or None


def excluded_prefixes() -> tuple[str, ...]:
    """Префиксы имён БД, которые не подлежат очистке (env DB_CLEAN_EXCLUDE_PREFIXES)."""
    raw = os.environ.get("DB_CLEAN_EXCLUDE_PREFIXES", "priem")
    return tuple(
        p.strip().lower().rstrip(".")
        for p in raw.split(",")
        if p.strip()
    )


def deal_candidates(databases: list[str]) -> dict[str, list[int]]:
    """Оставить только БД, чьё имя разбирается как клиент_<id сделок>."""
    prefixes = excluded_prefixes()
    candidates: dict[str, list[int]] = {}
    for name in databases:
        if name in SYSTEM_DBS:
            continue
        if any(name.lower().startswith(p) for p in prefixes):
            continue
        deal_ids = parse_candidate(name)
        if deal_ids:
            candidates[name] = deal_ids
    return candidates


def deal_id_list(candidates: dict[str, list[int]]) -> list[int]:
    """Номера сделок по всем кандидатам, без повторов, в порядке появления."""
    ids: list[int] = []
    for deal_ids in candidates.values():
        for i in deal_ids:
            if i not in ids:
                ids.append(i)
    return ids


def collect_candidates(
    candidates: dict[str, list[int]],
    deals: dict[int, dict],
    terminal: frozenset[str],
    allow_missing: bool = False,
) -> tuple[list[tuple[str, list[int]]], list[tuple[str, list[int], str]]]:
    """Разделить кандидатов на удаляемые и пропущенные с причиной пропуска.

    deals — сделки Б24 по ID; отсутствующий ID означает «сделки нет».
    Причины в returned skipped совпадают с текстом строк SKIPPED в логе.
    allow_missing=True — БД с не найденными сделками уходит в to_drop
    (без него — fail-closed пропуск).
    """
    to_drop: list[tuple[str, list[int]]] = []
    skipped: list[tuple[str, list[int], str]] = []
    for name in sorted(candidates):
        deal_ids = candidates[name]

        missing = [i for i in deal_ids if deals.get(i) is None]
        if missing:
            if allow_missing:
                to_drop.append((name, deal_ids))
                continue
            skipped.append((name, deal_ids, f"нет сделки в Б24 для {missing}"))
            continue

        unfinished = [i for i in deal_ids if not deal_finished(deals[i], terminal)]
        if unfinished:
            skipped.append(
                (name, deal_ids, f"не завершены: {deal_states(deals, unfinished)}")
            )
            continue

        to_drop.append((name, deal_ids))
    return to_drop, skipped


def b24_env_safe() -> None:
    """Прокидывает B24-секреты окружение, если B24_REPO/.env отсутствует.

    macOS: .env (имеет приоритет в b24_client) — поведение не меняется.
    Windows: секреты берутся из Credential Manager через notify.secret_get.
    """
    if (B24_ROOT / ".env").exists():
        return
    try:
        sys.path.insert(0, str(MONITORING_REPO))
        from notify import secret_get

        if not os.environ.get("B24_BASE_URL"):
            os.environ["B24_BASE_URL"] = secret_get("opencode.ts-b24.base-url")
        if not os.environ.get("B24_WEBHOOK_TOKEN"):
            os.environ["B24_WEBHOOK_TOKEN"] = secret_get("opencode.ts-b24.webhook-token")
    except Exception as ex:
        print(f"warning b24 secrets: {ex}", file=sys.stderr)


def fetch_deals(ids: list[int]):
    b24_env_safe()
    sys.path.insert(0, str(B24_ROOT / "scripts"))
    from b24_client import B24Client

    client = B24Client(env_file=None)
    deals: dict[int, dict] = {}
    if ids:
        try:
            rows = client.get_all(
                "crm.deal.list",
                {"filter": {"@ID": ids},
                 "select": ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID",
                            "STAGE_SEMANTIC_ID", "CLOSED"]},
            )
            deals = {int(r.get("ID")): r for r in rows if r.get("ID") is not None}
        except Exception as ex:
            print(f"warning crm.deal.list(@ID): {ex}", file=sys.stderr)
    for deal_id in ids:
        if deal_id in deals:
            continue
        try:
            res = client.call_gentle("crm.deal.get", {"id": deal_id}).get("result")
        except Exception as ex:
            res = None
            print(f"warning crm.deal.get({deal_id}): {ex}", file=sys.stderr)
        if isinstance(res, dict) and res.get("ID"):
            deals[int(res["ID"])] = res
    return deals


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Автоочистка БД aisql по стадиям заявок Б24")
    ap.add_argument("--commit", action="store_true",
                    help="реально удалять БД (по умолчанию dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="только показать")
    ap.add_argument("--host", default=AISQL_HOST, help="хост aisql")
    ap.add_argument("--limit", type=int, default=0,
                    help="максимум БД к обработке на одном сервере (0 = все)")
    ap.add_argument("--log", default=os.environ.get(
        "DB_CLEAN_LOG", "/var/log/corp-db-clean.log"))
    ap.add_argument("--signals", default=os.environ.get(
        "DB_CLEAN_SIGNALS", "/var/run/corp-vpn-signals"))
    ap.add_argument("--notify", action="store_true",
                     help="продакшн-уведомления (фактическое удаление) слать "
                          "в B24/Telegram напрямую (Windows, без corp-notify.sh)")
    ap.add_argument("--only-success", action="store_true",
                    help="удалять только по успешным сделкам "
                         "(STAGE_SEMANTIC_ID='S'), без проваленных")
    ap.add_argument("--delete-missing", action="store_true",
                    help="удалять БД, чьи сделки не найдены в Б24 "
                         "(вместо fail-closed пропуска)")
    ap.add_argument("--sweep", action="store_true",
                    help="очистить файлы логов (журнал) у всех найденных БД "
                         "на всех серверах; без удаления")
    ap.add_argument("--sweep-dry-run", action="store_true",
                    help="dry-run для --sweep: только список БД для очистки, "
                         "без выполнения очистки")
    args = ap.parse_args()

    terminal = terminal_semantics(args.only_success)

    commit = args.commit or os.environ.get("DB_CLEAN_COMMIT") == "1"
    if args.dry_run:
        commit = False

    log = Path(args.log)
    signals = Path(args.signals)
    notify = args.notify or os.environ.get("DB_CLEAN_NOTIFY") == "1"

    def emit(event: str, msg: str, *, send: bool = True) -> None:
        # Продакшн-ветка: только фактическое удаление идёт в B24/Telegram.
        signal(signals, event, msg)
        if notify and send:
            notify_direct(event, msg)
        notify_windows(event, msg)

    def emit_local(event: str, msg: str) -> None:
        # Непродакшн: сигнал для истории + Windows-тост, без B24/Telegram.
        signal(signals, event, msg)
        notify_windows(event, msg)

    stats: dict[str, dict] = {}

    def remember(host: str, *, removed: int, errors: int, names: list[str],
                 failed: list[str], sizes: dict[str, str] | None = None) -> None:
        stats[host] = {"removed": removed, "errors": errors,
                       "names": names, "failed": failed,
                       "sizes": sizes or {}}

    load_psa()
    try:
        targets: list[tuple[str, str]] = [(ENGINE_MSSQL, args.host)]
        seen_hosts = {args.host}
        for host_key in pgsql_targets():
            if host_key not in seen_hosts:
                targets.append((ENGINE_PGSQL, host_key))
                seen_hosts.add(host_key)
    except Exception as ex:
        msg = one_line(ex)
        log_line(log, f"FAIL сбор pgsql-таргетов из реестра PSA: {msg}")
        set_retry()
        emit_local("db-clean.fail",
                   f"реестр PSA недоступен (таргеты): {msg}")
        return 2

    clients = {ENGINE_MSSQL: get_mssql(), ENGINE_PGSQL: get_pgsql()}

    log_line(
        log,
        f"=== START commit={int(commit)} "
        f"hosts={[h for _e, h in targets]} "
        f"terminal={','.join(sorted(terminal))} ===",
    )

    # Шаг 1: precheck + список БД по каждому серверу.
    reachable: list[tuple[str, str, dict[str, list[int]]]] = []
    unreachable: list[tuple[str, str, str]] = []
    for engine, host in targets:
        try:
            precheck_err = precheck_network(host)
        except Exception as ex:
            precheck_err = one_line(ex)
        if precheck_err:
            log_line(log, f"FAIL precheck {host}: {precheck_err}")
            unreachable.append((engine, host, precheck_err))
            continue
        try:
            databases = list_databases(clients[engine], engine, host)
        except Exception as ex:
            msg = one_line(ex)
            log_line(log, f"FAIL {host} недоступен: {msg}")
            unreachable.append((engine, host, msg))
            continue
        reachable.append((engine, host, deal_candidates(databases)))
        remember(host, removed=0, errors=0, names=[], failed=[])

    # Шаг 1.5: sweep — очистка файлов логов у всех найденных БД (без удаления).
    if args.sweep or args.sweep_dry_run:
        sweep_cleaned: dict[str, list[tuple[str, str]]] = {}
        for engine, host, _candidates in reachable:
            try:
                if args.sweep_dry_run:
                    sweep_cleaned[host] = plan_sweep(clients[engine], engine, host)
                else:
                    sweep_cleaned[host] = sweep_logs(clients[engine], engine, host, log)
            except Exception as ex:
                log_line(log, f"SWEEP_FAIL host={host}: {one_line(ex)}")
                continue
        if args.sweep_dry_run:
            for host, entries in sorted(sweep_cleaned.items()):
                if not entries:
                    continue
                print(f"{host}: к очистке {len(entries)} БД")
                for name, size in entries:
                    print(f"  would-sweep {host} {name} ({size})")
        elif sweep_cleaned:
            emit("db-clean.sweep", sweep_message(sweep_cleaned))

    # Шаг 2: сделки Б24 один раз на все серверы.
    all_ids = sorted({
        deal_id
        for _e, _h, candidates in reachable
        for deal_id in deal_id_list(candidates)
    })
    deals = fetch_deals(all_ids) if all_ids else {}

    # Шаг 3: отбор кандидатов и удаление по серверам.
    allow_missing = args.delete_missing or os.environ.get(
        "DB_CLEAN_DELETE_MISSING") == "1"
    for engine, host, candidates in reachable:
        to_drop, skipped = collect_candidates(
            candidates, deals, terminal, allow_missing=allow_missing,
        )
        reasons = {name: reason for name, _ids, reason in skipped}
        for name in sorted(candidates):
            deal_ids = candidates[name]
            if name in reasons:
                log_line(log,
                         f"SKIPPED host={host} {name} deal={deal_ids} "
                         f"{reasons[name]}")
            else:
                log_line(
                    log,
                    f"CANDIDATE host={host} {name} deal={deal_ids} "
                    f"завершены: {deal_states(deals, deal_ids)}",
                )

        if args.limit > 0:
            to_drop = to_drop[:args.limit]

        names = [n for n, _ in to_drop]
        summary = (
            f"SUMMARY host={host} engine={engine} "
            f"candidates={len(candidates)} to_drop={len(to_drop)} "
            f"skipped={len(skipped)} dry_run={int(not commit)}"
        )
        log_line(log, summary)

        if not commit:
            if to_drop:
                # Dry-run — не продакшн: без B24/Telegram.
                emit_local(
                    "db-clean.dryrun",
                    f"{host}: к удалению {len(to_drop)} БД: "
                    f"{banner_list(names)}",
                )
            # Нулевой dry-run — только лог/консоль, без сигнала/уведомления.
            else:
                log_line(log, f"DRYRUN host={host}: к удалению 0 БД")
            print(summary)
            for n, d in to_drop:
                print(f"  would-drop [{host}] {n} (deal {d})")
            remember(host, removed=0, errors=0, names=names, failed=[])
            continue

        removed = 0
        errors = 0
        failed_names: list[str] = []
        for name, deal_ids in to_drop:
            try:
                drop_database(clients[engine], engine, host, name)
                removed += 1
                time.sleep(2)
                log_line(log, f"REMOVED host={host} {name} deal={deal_ids}")
            except Exception as ex:
                errors += 1
                failed_names.append(name)
                log_line(log,
                         f"ERROR host={host} {name} deal={deal_ids}: "
                         f"{one_line(ex)}")

        log_line(log, f"SUMMARY host={host} removed={removed} errors={errors}")

        sizes: dict[str, str] = {}
        for n in names:
            raw_sizes = clients[engine].database_sizes(host) or {}
            if n in raw_sizes:
                sizes[n] = fmt_size(int(raw_sizes[n] or 0))
        remember(host, removed=removed, errors=errors, names=names,
                 failed=failed_names, sizes=sizes)

        if errors:
            # Ошибки удаления — только лог и Windows-тост, без B24/Telegram.
            emit_local(
                "db-clean.fail",
                f"{host}: удалено {removed}, ошибок {errors}: "
                f"{banner_list(failed_names)}",
            )
        elif removed:
            log_line(log, f"OK host={host}: удалено {removed} БД")
        else:
            # Нулевой результат — только лог, без сигнала и уведомления.
            log_line(log, f"OK host={host}: удалено 0 БД")

    # Шаг 4: итог по всем серверам, retry, уведомления, соединения.
    fail = False
    if unreachable:
        fail = True
        set_retry()
        if os.environ.get("DB_CLEAN_RETRY") != "1":
            for _engine, host, msg in unreachable:
                # FAIL — только лог и Windows-тост, без B24/Telegram.
                emit_local("db-clean.fail", f"{host} недоступен: {msg}")

    total_removed = sum(st["removed"] for st in stats.values())
    total_errors = sum(st["errors"] for st in stats.values())
    if total_errors:
        fail = True
        set_retry()
    elif unreachable:
        # Недоступный сервер: маркер уже поднят — не сбрасываем его и не
        # шлём ok, чтобы retry-демон переподнял пропущенный сервер.
        pass
    elif total_removed:
        clear_retry()
        # Продакшн-ветка: B24/Telegram (+ Windows-тост внутри emit).
        emit("db-clean.ok", ok_message(stats))
    else:
        clear_retry()

    for engine in (ENGINE_MSSQL, ENGINE_PGSQL):
        try:
            clients[engine].close_all()
        except Exception:
            pass
    return 2 if fail else 0


if __name__ == "__main__":
    sys.exit(main())