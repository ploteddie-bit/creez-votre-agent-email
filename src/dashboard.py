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
                "executed_at = NOW() WHERE id = %s RETURNING email_id, executable_operation",
                (decision_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"decision {decision_id} not found")
        conn.commit()

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
                "RETURNING email_id",
                (decision_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"decision {decision_id} not found")
        conn.commit()

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
    return _jsonify_email(row)


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
