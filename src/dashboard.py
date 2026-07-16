"""Dashboard FastAPI - health + emails + decisions + WebSocket temps reel.

Suit la SPEC section 9 (Dashboard HTTP 24/7).

Architecture :
  - Bind sur 10.0.0.XXX:8000 (uuvicorn), JAMAIS 0.0.0.0
  - Caddy en reverse proxy : 8080 (HTTPS) -> 8000
  - CSP restrictive (middleware)
  - WebSocket pour le temps reel
  - Pas d'authentification (LAN = confiance, isolation reseau)

Endpoints REST :
  GET  /api/health            -> sante systeme
  GET  /api/emails            -> liste paginee
  GET  /api/emails/{id}       -> detail + similarites
  GET  /api/decisions         -> journal
  POST /api/decisions/{id}/approve
  POST /api/decisions/{id}/reject
  GET  /api/config            -> configuration actuelle
  PUT  /api/config            -> mise a jour configuration
  POST /api/sync              -> declenche sync Gmail manuelle
  GET  /api/stats             -> statistiques agregees

WebSocket :
  WS   /api/ws                -> evenements temps reel

Pages statiques :
  /, /mails, /decisions, /stats, /learning, /config
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import get_settings

logger = logging.getLogger(__name__)


# === Chemins ===
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = PROJECT_ROOT / "static"


# === Manager WebSocket ===

class WebSocketManager:
    """Gestionnaire de connexions WebSocket avec broadcast.

    En production avec plusieurs workers, remplacer par Redis pub/sub.
    Pour notre cas (LAN, 1 worker), le set en memoire suffit.
    """

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)
        logger.info("ws connected (total=%d)", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)
        logger.info("ws disconnected (total=%d)", len(self.active))

    async def broadcast(self, event_type: str, data: Any) -> None:
        """Envoie un evenement a toutes les connexions actives."""
        if not self.active:
            return
        message = json.dumps({"type": event_type, "data": data}, default=str)
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


# Instance globale (1 par process)
ws_manager = WebSocketManager()


# === Lifespan ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hook de demarrage/arret du serveur."""
    settings = get_settings()
    logger.info(
        "dashboard starting: bind=%s:%d env=%s p2_enabled=%s",
        settings.dashboard.bind_host,
        settings.dashboard.bind_port,
        settings.environment,
        settings.p2.enabled,
    )
    # Sanity check : on ne doit JAMAIS binder sur 0.0.0.0
    if settings.dashboard.bind_host == "0.0.0.0":
        raise RuntimeError(
            "DASHBOARD_BIND_HOST=0.0.0.0 REFUSE pour raisons de securite. "
            "Le dashboard ne doit etre visible que sur le LAN."
        )
    yield
    logger.info("dashboard shutting down")


# === Application FastAPI ===

app = FastAPI(
    title="Agent Mail 24/7",
    version="0.1.0",
    description="Dashboard du daemon de gestion d'emails par IA locale",
    lifespan=lifespan,
)

# CSP middleware (defense contre XSS)
@app.middleware("http")
async def add_csp(request: Request, call_next):
    """Ajoute le header Content-Security-Policy restrictif."""
    settings = get_settings()
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = settings.dashboard.csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# Sert les assets statiques (JS/CSS des pages dashboard) sous /static/.
# Doit etre monte APRES les routes HTML ci-dessous pour ne pas les masquer :
# FastAPI resout les routes dans l'ordre d'enregistrement, mais les routes
# @app.get explicites sont toujours prioritaires sur le mount StaticFiles
# car elles sont ajoutees avant. On le monte ici (avant la premiere route HTML)
# pour disponibilite, et les routes nommees restent prioritaires.
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


# === Endpoints : sante ===

@app.get("/api/health", tags=["health"])
async def health() -> dict[str, Any]:
    """Sante systeme pour le dashboard et les healthchecks externes.

    Combine :
      - DB PostgreSQL joignable
      - Ollama joignable
      - Gmail API joignable
      - Circuit-breaker de l'observer
      - Etat de sync_state
    """
    from src.db import healthcheck as db_health
    from src.observer import GmailObserver
    import httpx

    settings = get_settings()
    result: dict[str, Any] = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": settings.environment,
        "p2_enabled": settings.p2.enabled,
        "kill_switch": settings.is_kill_switch_on(),
        "checks": {},
    }

    # PostgreSQL
    result["checks"]["postgresql"] = {
        "reachable": db_health(),
        "host": settings.postgres.host,
    }

    # Ollama (test rapide : GET /api/tags)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.ollama.base_url}/api/tags")
        result["checks"]["ollama"] = {
            "reachable": r.status_code == 200,
            "models_count": len(r.json().get("models", [])),
        }
    except Exception as e:
        result["checks"]["ollama"] = {
            "reachable": False,
            "error": str(e),
        }

    # Gmail API : check via observer (sans faire d'appel reel)
    try:
        obs = GmailObserver()
        result["checks"]["gmail_observer"] = obs.health()
    except Exception as e:
        result["checks"]["gmail_observer"] = {"error": str(e)}

    # Statut global
    if not result["checks"]["postgresql"]["reachable"]:
        result["status"] = "degraded"
    if not result["checks"]["ollama"]["reachable"]:
        result["status"] = "degraded"

    return result


# === Endpoints : emails ===

@app.get("/api/emails", tags=["emails"])
async def list_emails(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sender: Optional[str] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Liste paginee des emails."""
    from src.db import get_connection

    where: list[str] = []
    params: list = []
    if sender:
        where.append("sender_email = %s")
        params.append(sender)
    if label:
        where.append("%s = ANY(labels)")
        params.append(label)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT id, sender, sender_email, subject,
               date_received, labels, is_read, is_starred
        FROM emails
        {where_sql}
        ORDER BY date_received DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM emails " + where_sql, params[:-2])
            total = cur.fetchone()[0]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_jsonify_email(r) for r in rows],
    }


@app.get("/api/emails/search", tags=["emails"])
async def search_emails(
    q: str = Query(..., min_length=1, max_length=500,
                  description="Texte a chercher (full-text)"),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Recherche full-text dans les emails (tsvector FR).

    NOTE : cette route DOIT etre definie AVANT /api/emails/{email_id}, sinon
    FastAPI capture "search" comme un email_id (les routes statiques sont
    matchees avant les routes parametrees lors de la resolution).
    """
    from src.search import HybridSearch

    hs = HybridSearch()
    results = hs.fulltext_search(q, limit=limit)

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }


@app.get("/api/emails/{email_id}", tags=["emails"])
async def get_email(email_id: str) -> dict[str, Any]:
    """Detail d'un email + emails similaires."""
    from src.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM emails WHERE id = %s", (email_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"email {email_id} not found")
            cols = [d[0] for d in cur.description]
            email = dict(zip(cols, row))

            # Decisions associees
            cur.execute(
                "SELECT id, phase, classification, executable_operation, "
                "user_approved, final_confidence, created_at "
                "FROM decision_journal WHERE email_id = %s ORDER BY created_at DESC",
                (email_id,),
            )
            cols = [d[0] for d in cur.description]
            decisions = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {
        "email": _jsonify_email(email, full=True),
        "decisions": [_jsonify_decision(d) for d in decisions],
    }


# === Endpoints : stats ===

@app.get("/api/stats", tags=["stats"])
async def get_stats(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Statistiques agregees sur N jours.

    - repartition_actions : count par executable_operation (decision_journal)
    - top_senders : top 10 expediteurs par volume
    - actions_par_jour : count par jour
    - counters : compteurs globaux
    """
    from src.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Repartition des actions (decision_journal)
            cur.execute(
                """
                SELECT executable_operation, COUNT(*) AS count
                FROM decision_journal
                WHERE created_at >= NOW() - (%s || ' days')::INTERVAL
                GROUP BY executable_operation
                ORDER BY count DESC
                """,
                (str(days),),
            )
            repartition_actions = {
                row[0]: row[1] for row in cur.fetchall()
            }

            # 2. Top 10 expediteurs par volume
            cur.execute(
                """
                SELECT sender_email, sender_domain, COUNT(*) AS count
                FROM emails
                WHERE date_received >= NOW() - (%s || ' days')::INTERVAL
                GROUP BY sender_email, sender_domain
                ORDER BY count DESC
                LIMIT 10
                """,
                (str(days),),
            )
            top_senders = [
                {"sender_email": r[0], "sender_domain": r[1], "count": r[2]}
                for r in cur.fetchall()
            ]

            # 3. Actions par jour (30 derniers jours)
            cur.execute(
                """
                SELECT DATE(created_at) AS day, COUNT(*) AS count
                FROM decision_journal
                WHERE created_at >= NOW() - (%s || ' days')::INTERVAL
                GROUP BY DATE(created_at)
                ORDER BY day ASC
                """,
                (str(days),),
            )
            actions_par_jour = [
                {"day": r[0].isoformat(), "count": r[1]}
                for r in cur.fetchall()
            ]

            # 4. Compteurs globaux
            cur.execute("SELECT COUNT(*) FROM emails")
            total_emails = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM decision_journal")
            total_decisions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM action_queue WHERE status='done'")
            total_actions_done = cur.fetchone()[0]

    return {
        "days": days,
        "repartition_actions": repartition_actions,
        "top_senders": top_senders,
        "actions_par_jour": actions_par_jour,
        "counters": {
            "total_emails": total_emails,
            "total_decisions": total_decisions,
            "total_actions_done": total_actions_done,
        },
    }


# === Endpoints : learning ===

@app.get("/api/learning", tags=["learning"])
async def get_learning(window: int = Query(100, ge=10, le=1000)) -> dict[str, Any]:
    """Metriques d'apprentissage P1/P2.

    - precision_par_action : ratio approved/rejected sur la fenetre
    - top_domains_appris : domaines avec le plus d'actions archive
    - progression_p2 : est-on pres des seuils ?
    - consecutive_rejections : actions temporairement desactivees
    """
    from src.decider import Decider
    from src.db import get_connection

    # Precision par action via Decider
    decider = Decider()
    window_stats = decider.get_window_stats()

    # Top domaines appris (domaines avec le plus d'emails archives)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sender_domain, COUNT(*) AS archived_count
                FROM emails
                WHERE is_archived = TRUE
                  AND sender_domain IS NOT NULL
                GROUP BY sender_domain
                ORDER BY archived_count DESC
                LIMIT 10
                """,
            )
            top_domains_appris = [
                {"domain": r[0], "archived_count": r[1]}
                for r in cur.fetchall()
            ]

            # Progression P2 (combien de decisions valides)
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE user_approved = TRUE) AS approved,
                    COUNT(*) FILTER (WHERE user_approved = FALSE) AS rejected
                FROM decision_journal
                WHERE phase IN ('P1', 'P2')
                  AND created_at >= NOW() - INTERVAL '30 days'
                """
            )
            row = cur.fetchone()
            approved = row[0] if row and len(row) > 0 and row[0] else 0
            rejected = row[1] if row and len(row) > 1 and row[1] else 0
            total = approved + rejected
            overall_precision = (approved / total) if total > 0 else 1.0

    return {
        "window": window,
        "window_stats": window_stats,
        "top_domains_appris": top_domains_appris,
        "progression_p2": {
            "approved_30d": approved,
            "rejected_30d": rejected,
            "overall_precision_30d": round(overall_precision, 3),
            "p2_enabled": decider.p2_enabled,
            "kill_switch": decider.is_kill_switch_on() if hasattr(decider, "is_kill_switch_on") else (not decider.p2_enabled),
        },
    }


# === Endpoints : decisions ===

@app.get("/api/decisions", tags=["decisions"])
async def list_decisions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    phase: Optional[str] = None,
    classification: Optional[str] = None,
    approved: Optional[bool] = None,
) -> dict[str, Any]:
    """Journal des decisions (P0/P1/P2)."""
    from src.db import get_connection

    where: list[str] = []
    params: list = []
    if phase:
        where.append("phase = %s")
        params.append(phase)
    if classification:
        where.append("classification = %s")
        params.append(classification)
    if approved is not None:
        where.append("user_approved = %s")
        params.append(approved)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT id, email_id, phase, classification, executable_operation,
               recommended_user_action, llm_confidence, final_confidence,
               user_approved, executed_at, execution_status, created_at
        FROM decision_journal
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM decision_journal " + where_sql, params[:-2])
            total = cur.fetchone()[0]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_jsonify_decision(r) for r in rows],
    }


@app.post("/api/decisions/{decision_id}/approve", tags=["decisions"])
async def approve_decision(decision_id: int) -> dict[str, Any]:
    """Approuve une decision P1 (lance l'execution via action_queue)."""
    from src.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decision_journal SET user_approved = TRUE, "
                "executed_at = NOW() WHERE id = %s "
                "RETURNING email_id, executable_operation",
                (decision_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"decision {decision_id} not found")
        conn.commit()

    # A3 : Tracking corrections (reset du compteur de rejections consecutives)
    try:
        from src.decider import Decider
        decider = Decider()
        decider.record_user_correction(
            email_id=row[0],
            action_type=row[1],
            was_correct=True,
        )
    except Exception as e:
        # Ne pas bloquer l'approbation si le tracking echoue
        import logging
        logging.getLogger(__name__).debug("record_user_correction failed: %s", e)

    # A4 : Si executable_operation != 'none', enqueue l'action
    if row[1] != "none":
        try:
            from src.action_worker import ActionWorker
            aw = ActionWorker()
            aw.enqueue_action(email_id=row[0], operation=row[1])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("enqueue_action failed: %s", e)

    # Broadcast WebSocket
    await ws_manager.broadcast("decision_approved", {
        "decision_id": decision_id,
        "email_id": row[0],
        "operation": row[1],
    })

    return {"ok": True, "decision_id": decision_id, "executed_operation": row[1]}


@app.post("/api/decisions/{decision_id}/reject", tags=["decisions"])
async def reject_decision(decision_id: int) -> dict[str, Any]:
    """Rejette une decision P1 (l'IA n'executera rien)."""
    from src.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decision_journal SET user_approved = FALSE WHERE id = %s "
                "RETURNING email_id, executable_operation",
                (decision_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"decision {decision_id} not found")
        conn.commit()

    # A3 : Tracking corrections (incremente le compteur)
    try:
        from src.decider import Decider
        decider = Decider()
        decider.record_user_correction(
            email_id=row[0],
            action_type=row[1],
            was_correct=False,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("record_user_correction failed: %s", e)

    await ws_manager.broadcast("decision_rejected", {
        "decision_id": decision_id,
        "email_id": row[0],
    })

    return {"ok": True, "decision_id": decision_id}


# === Endpoints : config ===

@app.get("/api/config", tags=["config"])
async def get_config() -> dict[str, Any]:
    """Configuration actuelle (lecture seule via API)."""
    from src.config import get_settings as _get
    s = _get()
    return {
        "environment": s.environment,
        "p2_enabled": s.p2.enabled,
        "p2_max_daily_actions": s.p2.max_daily_actions,
        "polling_interval_seconds": s.polling.interval_seconds,
        "ollama_llm_model": s.ollama.llm_model,
        "ollama_embedding_model": s.ollama.embedding_model,
        "vacation_mode": s.p2.vacation_mode,
    }


@app.put("/api/config", tags=["config"])
async def update_config(body: dict[str, Any]) -> dict[str, Any]:
    """Mise a jour de la configuration (P2, mode vacances, etc.).

    Note : pour l'instant, les changements sont en memoire seulement.
    Une version production les persisterait dans une table de config.
    """
    # Cette implementation naive met a jour les settings en memoire
    # Pour la production, on sauvegarderait dans une table dediee
    from src.config import reset_settings, get_settings as _get
    s = _get()

    if "p2_enabled" in body:
        s.p2.enabled = bool(body["p2_enabled"])
    if "vacation_mode" in body:
        s.p2.vacation_mode = bool(body["vacation_mode"])

    return await get_config()


# === Endpoints : sync ===

@app.post("/api/sync", tags=["sync"])
async def trigger_sync() -> dict[str, Any]:
    """Declenche une sync Gmail manuelle (retourne immediatement)."""
    from src.observer import GmailObserver
    obs = GmailObserver()
    try:
        # On lance en arriere-plan, on retourne juste "started"
        # En production on utiliserait un BackgroundTasks
        ingested = obs.sync_delta()
        return {"status": "completed", "ingested": ingested}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# === WebSocket ===

@app.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket pour evenements temps reel.

    Events envoyes : new_mail, new_decision, decision_approved,
    decision_rejected, sandbox_alert, health_update.
    """
    await ws_manager.connect(ws)
    try:
        # Ping periodique pour detecter les deconnexions
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# === Pages statiques ===

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    """Page d'accueil (servie depuis static/index.html)."""
    return HTMLResponse(_read_static("index.html"))


@app.get("/mails", response_class=HTMLResponse, include_in_schema=False)
async def mails_page() -> HTMLResponse:
    return HTMLResponse(_read_static("mails.html"))


@app.get("/decisions", response_class=HTMLResponse, include_in_schema=False)
async def decisions_page() -> HTMLResponse:
    return HTMLResponse(_read_static("decisions.html"))


@app.get("/stats", response_class=HTMLResponse, include_in_schema=False)
async def stats_page() -> HTMLResponse:
    return HTMLResponse(_read_static("stats.html"))


@app.get("/learning", response_class=HTMLResponse, include_in_schema=False)
async def learning_page() -> HTMLResponse:
    return HTMLResponse(_read_static("learning.html"))


@app.get("/config", response_class=HTMLResponse, include_in_schema=False)
async def config_page() -> HTMLResponse:
    return HTMLResponse(_read_static("config.html"))


@app.get("/cours", response_class=HTMLResponse, include_in_schema=False)
async def cours_page() -> HTMLResponse:
    return HTMLResponse(_read_static("cours-agent-mail-24-7.html"))


@app.get("/prompts", response_class=HTMLResponse, include_in_schema=False)
async def prompts_page() -> HTMLResponse:
    """Page des prompts par subagent (referencée dans la navigation)."""
    return HTMLResponse(_read_static("prompts.html"))


@app.get("/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_page() -> HTMLResponse:
    """Page du plan d'action (referencée dans la navigation)."""
    return HTMLResponse(_read_static("plan.html"))


# === Helpers ===

def _read_static(filename: str) -> str:
    """Lit un fichier HTML depuis static/. Retourne 404 si manquant."""
    path = STATIC_DIR / filename
    if not path.exists():
        # Fallback : rediriger vers la racine
        return (
            "<!DOCTYPE html><html><head><meta http-equiv='refresh' "
            "content='0; url=/'></head><body>Page non trouvee, "
            f"redirection... <a href='/'>{filename}</a></body></html>"
        )
    return path.read_text(encoding="utf-8")


def _jsonify_email(row: dict, *, full: bool = False) -> dict[str, Any]:
    """Convertit un row PostgreSQL en JSON-safe (datetime, etc.)."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "__html__"):  # markupsafe
            out[k] = str(v)
        else:
            out[k] = v
    if not full and "body_html" in out:
        out.pop("body_html", None)
        out.pop("raw_headers", None)
    return out


def _jsonify_decision(row: dict) -> dict[str, Any]:
    """Convertit une decision en JSON-safe + ajoute un rationale lisible."""
    from src.rationale import build_rationale
    from src.models import MailDecision

    out = _jsonify_email(row)

    # Extraire le rule_name du reason si applicable (format "rule:NAME")
    reason = row.get("reason") or ""
    rule_name = None
    if reason.startswith("rule:"):
        rule_name = reason[5:].split("|")[0].strip()

    # Reconstruire une MailDecision partielle pour le rationale
    try:
        decision = MailDecision(
            classification=row.get("classification", "unknown"),
            executable_operation=row.get("executable_operation", "none"),
            recommended_user_action=row.get("recommended_user_action", "none"),
            confidence=row.get("final_confidence") or row.get("llm_confidence") or 0.0,
            reason=reason,
        )
        out["rationale"] = build_rationale(
            decision,
            rule_name=rule_name,
            sender_domain=row.get("sender_domain"),
            similar_count=0,  # pas stocke, on approxime
        )
    except Exception:
        # Fallback : rationale minimal
        out["rationale"] = (
            f"Decision: {out.get('executable_operation', 'none')} "
            f"({out.get('classification', 'unknown')}, "
            f"conf={out.get('final_confidence', 0):.0%})"
        )
    return out


# === Entry point pour uvicorn ===

def run() -> None:
    """Lance le dashboard avec uvicorn."""
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "src.dashboard:app",
        host=settings.dashboard.bind_host,
        port=settings.dashboard.bind_port,
        log_level=settings.environment == "development" and "debug" or "info",
        reload=False,
    )


if __name__ == "__main__":
    run()
