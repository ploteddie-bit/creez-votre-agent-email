"""Backfill IMAP resumable : ingère l'historique Gmail par petits lots.

Pensé pour tourner de nuit (quota de bande passante IMAP Gmail) :
chaque exécution ingère au plus `--limit` messages sous le watermark
UID stocké dans `configs/sync_progress.json`, puis met à jour le
watermark. Idempotent (UPSERT côté ingester) et robuste aux nouveaux
mails (reprise par UID, pas par offset).

Usage (depuis la racine du projet) :
    python scripts/sync_backfill.py --limit 800 --workers 8

Sortie : une ligne JSON {"page", "ingested", "failed", "seconds",
"rate_msg_s", "new_watermark_uid", "remaining_estimate", "done"}.
Quand "done" est true, l'historique est complet — la tâche planifiée
peut être désactivée.
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

QUERY = "newer_than:6m -label:spam -label:promotions"
DEFAULT_PROGRESS = (Path(__file__).resolve().parent.parent
                    / "configs" / "sync_progress.json")

_thread_local = threading.local()
_clients: list = []
_clients_lock = threading.Lock()


def _thread_client():
    """Un IMAPClient par thread (sa propre connexion IMAP)."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        from src.imap_client import IMAPClient
        client = IMAPClient()
        with _clients_lock:
            _clients.append(client)
        _thread_local.client = client
    return client


def _fetch(msg_id: str):
    try:
        return _thread_client().get_message(msg_id)
    except Exception:
        return None


def _close_all() -> None:
    with _clients_lock:
        for client in _clients:
            try:
                client.close()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--progress-file", default=str(DEFAULT_PROGRESS))
    args = ap.parse_args()

    from src.observer import GmailObserver
    from src.parser import parse_raw_message
    from src.models import EmailInDB

    progress_path = Path(args.progress_file)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    watermark = int(progress["watermark_uid"])

    obs = GmailObserver()
    client = obs.gmail_client

    t0 = time.time()
    data = client._run("UID SEARCH", lambda c: c.uid(
        "SEARCH", "CHARSET", "UTF-8", "X-GM-RAW", f'"{QUERY}"'))
    candidates = sorted(
        (int(u) for u in (data[0].split() if data and data[0] else [])),
        reverse=True)
    below = [u for u in candidates if u < watermark]
    todo = below[:args.limit]

    if not todo:
        print(json.dumps({"done": True, "remaining_estimate": 0,
                          "ingested": 0, "failed": 0}))
        return

    messages = client._fetch_msgids(todo)

    # 1. FETCH parallèle (ordre préservé par ex.map)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        raws = list(ex.map(_fetch, [m["id"] for m in messages]))

    # 2. Parse + UPSERT séquentiels (ordre des UID, DB thread-safe)
    ok, ko = 0, 0
    for raw in raws:
        if raw is None:
            ko += 1
            continue
        try:
            parsed = parse_raw_message(raw)
            email = EmailInDB(**parsed)
            if obs.ingester.ingest_email(email):
                ok += 1
            else:
                ko += 1
        except Exception:
            ko += 1
    _close_all()

    new_watermark = todo[-1]
    progress_path.write_text(
        json.dumps({"watermark_uid": new_watermark}), encoding="utf-8")

    dt = time.time() - t0
    remaining = len(below) - len(todo)
    print(json.dumps({
        "done": False,
        "page": len(messages),
        "ingested": ok,
        "failed": ko,
        "seconds": round(dt, 1),
        "rate_msg_s": round(len(messages) / dt, 2) if dt else None,
        "new_watermark_uid": new_watermark,
        "remaining_estimate": remaining,
    }))


if __name__ == "__main__":
    main()
