"""Point d'entrée du daemon email-learner.

Usage :
    python -m src.main                  # démarre le daemon complet
    python -m src.main --sync-once      # sync Gmail une seule fois
    python -m src.main --embed 100      # embedde 100 emails non traités
    python -m src.main --health         # affiche /api/health et sort
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

# Permettre l'import de `src.*` depuis la racine du projet
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings  # noqa: E402

logger = logging.getLogger("email-learner")


def setup_logging(debug: bool = False) -> None:
    """Configure le logging structuré du daemon."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # Réduire le bruit des libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_sync_once(args: argparse.Namespace) -> int:
    """Une seule passe de sync Gmail (placeholder — subagent 2)."""
    logger.warning("gmail-sync non encore implémenté — voir PROMPTS agent 2")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    """Embedde N emails non traités."""
    from src.embedder import Embedder
    embedder = Embedder()
    done = embedder.embed_unprocessed(limit=args.count)
    logger.info("embedded %d emails", done)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Affiche l'état de santé et sort."""
    from src.db import healthcheck as db_health

    settings = get_settings()
    health = {
        "environment": settings.environment,
        "p2_enabled": settings.p2.enabled,
        "kill_switch": settings.is_kill_switch_on(),
        "db_reachable": db_health(),
        "ollama_url": settings.ollama.base_url,
        "ollama_model": settings.ollama.llm_model,
        "embedding_model": settings.ollama.embedding_model,
    }
    print(json.dumps(health, indent=2, default=str))
    return 0 if health["db_reachable"] else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    """Boucle principale du daemon (P0)."""
    logger.info("starting email-learner daemon (env=%s)", get_settings().environment)
    logger.info("p2_enabled=%s kill_switch=%s",
                get_settings().p2.enabled,
                get_settings().is_kill_switch_on())
    logger.warning("daemon loop not yet implemented — observer/ingester/embedder/worker")
    logger.info("see PROMPTS-agent-mail.md agents 2 (gmail-sync), 3 (sandbox), 4 (parser), 5 (embedder)")

    stop = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal stop
        logger.info("received signal %s — shutting down", signum)
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not stop:
        time.sleep(1)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construit le parser CLI."""
    parser = argparse.ArgumentParser(
        prog="email-learner",
        description="Agent Mail 24/7 — daemon local autonome",
    )
    parser.add_argument("--debug", action="store_true", help="Active les logs DEBUG")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon", help="Démarre le daemon complet (boucle infinie)")
    sub.add_parser("sync-once", help="Lance une sync Gmail ponctuelle")
    sub.add_parser("health", help="Affiche l'état de santé et sort")

    p_embed = sub.add_parser("embed", help="Embedde N emails non traités")
    p_embed.add_argument("count", type=int, nargs="?", default=100,
                         help="Nombre max d'emails à embedder (défaut: 100)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(debug=args.debug)

    if args.command in (None, "daemon"):
        return cmd_daemon(args)
    if args.command == "sync-once":
        return cmd_sync_once(args)
    if args.command == "embed":
        return cmd_embed(args)
    if args.command == "health":
        return cmd_health(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
