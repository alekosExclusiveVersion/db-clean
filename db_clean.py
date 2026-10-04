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

Режимы: --dry-run (по умолчанию) показывает кандидатов, --commit удаляет.
Юзер-агент corp-notify.sh шлёт события db-clean.* в macOS-баннер, B24 и Telegram.
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
SYSTEM_DBS = frozenset(
    ("master", "tempdb", "model", "msdb",
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
    """Шлёт событие в B24/Telegram напрямую (без macOS-агента corp-notify.sh).

    Используется на Windows, где нет signal-файла и юзер-агента.
    """
    try:
        sys.path.insert(0, str(MONITORING_REPO))
        from notify import notify_all

        title = "db-clean: " + event.removeprefix("db-clean.")
        notify_all(title, msg)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} notify {event}: {msg}")
    except Exception as ex:
        print(f"notify error {event}: {ex}", file=sys.stderr)


def banner_list(names: list[str]) -> str:
    if len(names) <= BANNER_MAX_NAMES:
        return ", ".join(names)
    head = names[:BANNER_MAX_NAMES]
    return ", ".join(head) + f", и ещё {len(names) - BANNER_MAX_NAMES}"


def one_line(value) -> str:
    return " ".join(str(value).split())


def precheck_network(host: str) -> str | None:
    """Быстрая предпроверка сети до SQL-хоста, без долгого TDS-таймаута.

    Возвращает None если сеть в порядке, иначе строку причины:
    DNS не резолвится (VPN/корп.DNS недоступен) либо нет ping
    (нет маршрута в корпсеть). Отключается env DB_CLEAN_SKIP_PRECHECK=1.
    """
    if os.environ.get("DB_CLEAN_SKIP_PRECHECK") == "1":
        return None
    server = host.split("\\")[0].split(",")[0].strip() or host
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


def list_databases(client, host: str) -> list[str]:
    rows = client.query(host, "SELECT name FROM sys.databases")
    return sorted(r.get("name") for r in rows if r.get("name"))


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


def deal_candidates(databases: list[str]) -> dict[str, list[int]]:
    """Оставить только БД, чьё имя разбирается как клиент_<id сделок>."""
    candidates: dict[str, list[int]] = {}
    for name in databases:
        if name in SYSTEM_DBS:
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
) -> tuple[list[tuple[str, list[int]]], list[tuple[str, list[int], str]]]:
    """Разделить кандидатов на удаляемые и пропущенные с причиной пропуска.

    deals — сделки Б24 по ID; отсутствующий ID означает «сделки нет».
    Причины в returned skipped совпадают с текстом строк SKIPPED в логе.
    """
    to_drop: list[tuple[str, list[int]]] = []
    skipped: list[tuple[str, list[int], str]] = []
    for name in sorted(candidates):
        deal_ids = candidates[name]

        missing = [i for i in deal_ids if deals.get(i) is None]
        if missing:
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
                    help="максимум БД к обработке (0 = все)")
    ap.add_argument("--log", default=os.environ.get(
        "DB_CLEAN_LOG", "/var/log/corp-db-clean.log"))
    ap.add_argument("--signals", default=os.environ.get(
        "DB_CLEAN_SIGNALS", "/var/run/corp-vpn-signals"))
    ap.add_argument("--notify", action="store_true",
                    help="дальше слать события в B24/Telegram напрямую "
                         "(Windows, без corp-notify.sh)")
    ap.add_argument("--only-success", action="store_true",
                    help="удалять только по успешным сделкам "
                         "(STAGE_SEMANTIC_ID='S'), без проваленных")
    args = ap.parse_args()

    terminal = terminal_semantics(args.only_success)

    commit = args.commit or os.environ.get("DB_CLEAN_COMMIT") == "1"
    if args.dry_run:
        commit = False

    log = Path(args.log)
    signals = Path(args.signals)
    notify = args.notify or os.environ.get("DB_CLEAN_NOTIFY") == "1"
    notify_on_delete = os.environ.get("DB_CLEAN_NOTIFY_ON_DELETE") == "1"

    def emit(event: str, msg: str, *, send: bool = True) -> None:
        signal(signals, event, msg)
        if notify and send:
            notify_direct(event, msg)

    load_psa()
    mssql = get_mssql()

    log_line(
        log,
        f"=== START commit={int(commit)} host={args.host} "
        f"terminal={','.join(sorted(terminal))} ===",
    )

    try:
        precheck_err = precheck_network(args.host)
    except Exception as ex:
        precheck_err = one_line(ex)
    if precheck_err:
        log_line(log, f"FAIL precheck aisql недоступен: {precheck_err}")
        set_retry()
        if os.environ.get("DB_CLEAN_RETRY") != "1":
            # FAIL уведомляем всегда, даже при DB_CLEAN_NOTIFY_ON_DELETE=1:
            # иначе обрыв сети даёт тишину до следующих суток.
            emit("db-clean.fail", f"aisql недоступен (precheck): {precheck_err}")
        return 2

    try:
        databases = list_databases(mssql, args.host)
    except Exception as ex:
        msg = one_line(ex)
        log_line(log, f"FAIL aisql недоступен: {msg}")
        set_retry()
        if os.environ.get("DB_CLEAN_RETRY") != "1":
            # FAIL уведомляем всегда, даже при DB_CLEAN_NOTIFY_ON_DELETE=1.
            emit("db-clean.fail", f"aisql недоступен: {msg}")
        return 2
    clear_retry()

    candidates = deal_candidates(databases)
    ids = deal_id_list(candidates)
    deals = fetch_deals(ids) if ids else {}

    to_drop, skipped = collect_candidates(candidates, deals, terminal)
    reasons = {name: reason for name, _ids, reason in skipped}
    for name in sorted(candidates):
        deal_ids = candidates[name]
        if name in reasons:
            log_line(log, f"SKIPPED {name} deal={deal_ids} {reasons[name]}")
        else:
            log_line(
                log,
                f"CANDIDATE {name} deal={deal_ids} "
                f"завершены: {deal_states(deals, deal_ids)}",
            )

    if args.limit > 0:
        to_drop = to_drop[:args.limit]

    names = [n for n, _ in to_drop]
    summary = (
        f"SUMMARY candidates={len(candidates)} to_drop={len(to_drop)} "
        f"skipped={len(skipped)} dry_run={int(not commit)}"
    )
    log_line(log, summary)

    if not commit:
        if to_drop:
            emit("db-clean.dryrun",
                 f"к удалению {len(to_drop)} БД: {banner_list(names)}")
        else:
            emit("db-clean.dryrun", "к удалению 0 БД")
        print(summary)
        for n, d in to_drop:
            print(f"  would-drop {n} (deal {d})")
        return 0

    removed = 0
    errors = 0
    failed_names: list[str] = []
    for name, deal_ids in to_drop:
        try:
            mssql.drop_database(args.host, name)
            removed += 1
            time.sleep(2)
            log_line(log, f"REMOVED {name} deal={deal_ids}")
        except Exception as ex:
            errors += 1
            failed_names.append(name)
            log_line(log, f"ERROR {name} deal={deal_ids}: {one_line(ex)}")

    log_line(log, f"SUMMARY removed={removed} errors={errors}")

    if errors:
        emit("db-clean.fail",
             f"удалено {removed}, ошибок {errors}: {banner_list(failed_names)}")
        set_retry()
    elif removed:
        emit("db-clean.ok",
             f"aisql server очищен, удалены бд: {banner_list(names)}")
        clear_retry()
    else:
        emit("db-clean.ok", "удалено 0 БД", send=not notify_on_delete)
        clear_retry()

    try:
        mssql.close_all()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())