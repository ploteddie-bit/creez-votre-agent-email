"""Point d'entrée du daemon email-learner.

Boucle principale :
  1. Sync Gmail (full au 1er lancement, delta ensuite)
  2. Process les nouveaux emails (Recommender.process_new_emails)
  3. Embedde les emails sans embedding
  4. Action worker consomme la queue (en parallele)
  5. Sleep puis recommence

Usage :
    python -m src.main                       # daemon en boucle infinie
    python -m src.main sync                  # sync Gmail une fois
    python -m src.main sync --max 2000       # sync avec max custom
    python -m src.main embed [N]            # embedde N emails (defaut 100)
    python -m src.main process [N]           # process N emails (defaut 50)
    python -m src.main health                # JSON health et sort
    python -m src.main dashboard             # lance le dashboard FastAPI
    python -m src.main setup-oauth           # guide pour configurer OAuth
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

# Permettre l'import de `src.*` depuis la racine du projet
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings  # noqa: E402

logger = logging.getLogger("email-learner")


def setup_logging(debug: bool = False) -> None:
    """Configure le logging du daemon."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # Reduire le bruit des libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ============================================================
# Sous-commandes
# ============================================================

def cmd_sync(args: argparse.Namespace) -> int:
    """Une seule passe de sync Gmail (full au 1er lancement, delta ensuite)."""
    from src.observer import GmailObserver
    try:
        obs = GmailObserver()
        if args.full:
            logger.info("starting FULL sync (max=%d)...", args.max)
            ingested = obs.sync_full(max_results=args.max)
        else:
            logger.info("starting DELTA sync...")
            ingested = obs.sync_delta()
        logger.info("sync done: %d emails ingested", ingested)
        return 0
    except Exception as e:
        logger.error("sync failed: %s", e)
        return 1


def cmd_embed(args: argparse.Namespace) -> int:
    """Embedde N emails non traites."""
    try:
        from src.embedder import Embedder
        embedder = Embedder()
        done = embedder.embed_unprocessed(limit=args.count)
        logger.info("embedded %d emails", done)
        return 0
    except Exception as e:
        logger.error("embed failed: %s", e)
        return 1


def cmd_process(args: argparse.Namespace) -> int:
    """Process N emails qui n'ont pas encore de decision (P0 -> P1)."""
    try:
        from src.recommender import process_new_emails
        done = process_new_emails(batch_size=args.count)
        logger.info("processed %d emails (P0 -> P1)", done)
        return 0
    except Exception as e:
        logger.error("process failed: %s", e)
        return 1


def cmd_health(args: argparse.Namespace) -> int:
    """Affiche l'etat de sante complet et sort."""
    from src.db import healthcheck as db_health

    settings = get_settings()
    health = {
        "environment": settings.environment,
        "p2_enabled": settings.p2.enabled,
        "kill_switch": settings.is_kill_switch_on(),
        "vacation_mode": settings.p2.vacation_mode,
        "db_reachable": db_health(),
        "ollama_url": settings.ollama.base_url,
        "ollama_llm_model": settings.ollama.llm_model,
        "embedding_model": settings.ollama.embedding_model,
        "polling_interval_seconds": settings.polling.interval_seconds,
        "max_daily_actions": settings.p2.max_daily_actions,
    }

    # Ajouter l'etat de l'observer si dispo (sans planter si DB down)
    try:
        from src.observer import GmailObserver
        obs = GmailObserver()
        health["observer"] = obs.health()
    except Exception as e:
        health["observer"] = {"error": str(e)}

    # Ajouter les stats de la queue d'actions
    try:
        from src.action_worker import ActionWorker
        aw = ActionWorker()
        health["action_queue"] = aw.stats()
    except Exception as e:
        health["action_queue"] = {"error": str(e)}

    print(json.dumps(health, indent=2, default=str))
    return 0 if health["db_reachable"] else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Lance le dashboard FastAPI."""
    try:
        import uvicorn
        from src.dashboard import app
        settings = get_settings()
        logger.info("starting dashboard on %s:%d",
                    settings.dashboard.bind_host, settings.dashboard.bind_port)
        uvicorn.run(
            app,
            host=settings.dashboard.bind_host,
            port=settings.dashboard.bind_port,
            log_level="debug" if args.debug else "info",
        )
        return 0
    except Exception as e:
        logger.error("dashboard failed: %s", e)
        return 1


def cmd_setup_oauth(args: argparse.Namespace) -> int:
    """Guide interactif pour configurer OAuth Gmail."""
    print("=" * 60)
    print("Setup OAuth Gmail - Agent Mail 24/7")
    print("=" * 60)
    print()
    print("Pour configurer OAuth, vous devez :")
    print()
    print("1. Creer un projet Google Cloud Console :")
    print("   https://console.cloud.google.com/")
    print()
    print("2. Activer l'API Gmail :")
    print("   APIs & Services > Library > chercher 'Gmail API' > Enable")
    print()
    print("3. Creer un ecran de consentement OAuth :")
    print("   APIs & Services > OAuth consent screen")
    print("   - User type: External")
    print("   - Scopes: https://www.googleapis.com/auth/gmail.modify")
    print("   - Test users: ajouter votre email")
    print()
    print("4. Creer un identifiant OAuth 2.0 :")
    print("   APIs & Services > Credentials > Create Credentials > OAuth client ID")
    print("   - Application type: Desktop app")
    print("   - Download JSON")
    print()
    print("5. Copier le JSON telecharge vers :")
    print("   configs/gmail-credentials.json")
    print()
    print("6. Editer configs/.env et definir :")
    print("   EMAIL_LEARNER_GMAIL_CLIENT_ID=<votre_client_id>")
    print("   EMAIL_LEARNER_GMAIL_CLIENT_SECRET=<votre_client_secret>")
    print()
    print("7. Lancer le daemon : il fera le flow OAuth au premier demarrage")
    print("   python -m src.main")
    print()
    print("=" * 60)
    return 0


# ============================================================
# Boucle daemon
# ============================================================

# Flag global pour demander l'arret (sigint/sigterm)
_stop_requested = False


def _handle_signal(signum: int, frame: object) -> None:
    """Handler pour SIGINT/SIGTERM : demande l'arret propre."""
    global _stop_requested
    logger.info("received signal %s — requesting stop", signum)
    _stop_requested = True


def _run_one_cycle(interval_seconds: int) -> dict[str, int]:
    """Execute un cycle complet de la boucle daemon.

    Returns: dict avec les compteurs (pour les logs / healthcheck).
    """
    from src.action_worker import ActionWorker
    from src.embedder import Embedder
    from src.observer import GmailObserver
    from src.recommender import process_new_emails

    counters = {"synced": 0, "processed": 0, "embedded": 0, "executed": 0}

    # 1. Sync Gmail (delta ou full si 1er lancement)
    try:
        obs = GmailObserver()
        if obs.get_sync_state() and obs.get_sync_state().get("last_history_id"):
            counters["synced"] = obs.sync_delta()
        else:
            counters["synced"] = obs.sync_full()
    except Exception as e:
        logger.warning("sync cycle failed (will retry next cycle): %s", e)

    # 2. Embedde les emails sans embedding
    try:
        embedder = Embedder()
        counters["embedded"] = embedder.embed_unprocessed(limit=100)
    except Exception as e:
        logger.warning("embed cycle failed: %s", e)

    # 3. Process les emails sans decision (P0 -> P1, peut declencher P2)
    try:
        counters["processed"] = process_new_emails(batch_size=50)
    except Exception as e:
        logger.warning("process cycle failed: %s", e)

    # 4. Worker execute les actions en queue (max 30s)
    try:
        aw = ActionWorker()
        counters["executed"] = aw.run(max_iterations=10)
        aw.request_stop()
    except Exception as e:
        logger.warning("worker cycle failed: %s", e)

    return counters


def cmd_daemon(args: argparse.Namespace) -> int:
    """Boucle principale du daemon (P0/P1/P2)."""
    global _stop_requested
    _stop_requested = False

    settings = get_settings()
    interval = settings.polling.interval_seconds

    logger.info("=" * 60)
    logger.info("starting email-learner daemon")
    logger.info("  environment: %s", settings.environment)
    logger.info("  polling interval: %ds", interval)
    logger.info("  p2_enabled: %s (kill_switch: %s)",
                settings.p2.enabled, settings.is_kill_switch_on())
    logger.info("  ollama: %s (%s)", settings.ollama.base_url, settings.ollama.llm_model)
    logger.info("=" * 60)

    # Signaux : arret propre
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle_count = 0
    while not _stop_requested:
        cycle_count += 1
        logger.info("=== cycle %d starting ===", cycle_count)
        cycle_start = time.time()
        try:
            counters = _run_one_cycle(interval)
        except Exception as e:
            logger.exception("cycle %d crashed: %s", cycle_count, e)
            counters = {}

        duration = time.time() - cycle_start
        logger.info(
            "=== cycle %d done in %.1fs: synced=%d processed=%d embedded=%d executed=%d ===",
            cycle_count, duration,
            counters.get("synced", 0),
            counters.get("processed", 0),
            counters.get("embedded", 0),
            counters.get("executed", 0),
        )

        # Attendre l'intervalle, en sortie rapide si signal recu
        if not _stop_requested:
            for _ in range(interval):
                if _stop_requested:
                    break
                time.sleep(1)

    logger.info("daemon stopped cleanly after %d cycles", cycle_count)
    return 0


# ============================================================
# Parser
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    """Construit le parser CLI."""
    parser = argparse.ArgumentParser(
        prog="email-learner",
        description="Agent Mail 24/7 — daemon local autonome de gestion d'emails",
    )
    parser.add_argument("--debug", action="store_true", help="Active les logs DEBUG")
    sub = parser.add_subparsers(dest="command")

    # daemon (defaut)
    sub.add_parser("daemon", help="Demarre le daemon en boucle infinie (defaut)")

    # sync
    p_sync = sub.add_parser("sync", help="Lance une sync Gmail ponctuelle")
    p_sync.add_argument("--full", action="store_true",
                        help="Force un sync full (au lieu de delta)")
    p_sync.add_argument("--max", type=int, default=2000,
                        help="Nombre max d'emails pour un sync full (defaut: 2000)")

    # embed
    p_embed = sub.add_parser("embed", help="Embedde N emails non traites")
    p_embed.add_argument("count", type=int, nargs="?", default=100,
                        help="Nombre max d'emails a embedder (defaut: 100)")

    # process
    p_process = sub.add_parser("process", help="Process N emails (P0 -> P1)")
    p_process.add_argument("count", type=int, nargs="?", default=50,
                           help="Taille du batch (defaut: 50)")

    # health
    sub.add_parser("health", help="Affiche l'etat de sante et sort")

    # dashboard
    sub.add_parser("dashboard", help="Lance le dashboard FastAPI (Caddy en front)")

    # setup-oauth
    sub.add_parser("setup-oauth",
                   help="Affiche le guide pour configurer OAuth Gmail")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(debug=args.debug)

    if args.command in (None, "daemon"):
        return cmd_daemon(args)
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "embed":
        return cmd_embed(args)
    if args.command == "process":
        return cmd_process(args)
    if args.command == "health":
        return cmd_health(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)
    if args.command == "setup-oauth":
        return cmd_setup_oauth(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
