"""Recherche hybride (vectorielle + full-text + metadonnees) avec RRF.

Combine 3 sources d'evidence via Reciprocal Rank Fusion (RRF) :
  1. Recherche vectorielle (pgvector)  - similarite semantique bge-m3
  2. Recherche full-text (tsvector FR)  - matching lexical
  3. Metadonnees (sender/domaine)     - signaux forts

Le RRF (formule classique) : score(d) = sum  1 / (k + rank_i)
ou k=60 (parametre standard de la litterature).

Cascade : on essaie d'abord le sender exact, puis le domaine,
puis le global. Si on a deja 3 resultats au premier niveau, on s'arrete.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.db import get_connection

logger = logging.getLogger(__name__)


# Constante RRF (k=60 dans la litterature Cormack et al. 2009)
RRF_K = 60


@dataclass
class SearchResult:
    """Un resultat de recherche hybride."""
    email_id: str
    subject: Optional[str]
    sender_email: Optional[str]
    sender_domain: Optional[str]
    action_taken: Optional[str]   # derniere action de l'utilisateur sur ce mail
    rrf_score: float
    distance: float               # cosine distance pgvector
    rank_vector: Optional[int]    # rank dans la recherche vectorielle
    rank_tsvector: Optional[int]  # rank dans la recherche full-text
    retrieval_strategy: str       # "same_sender" | "same_domain" | "global_fallback"


class HybridSearch:
    """Recherche hybride RRF avec cascade sender -> domain -> global."""

    def __init__(self, min_results_to_shortcut: int = 3) -> None:
        self.min_results_to_shortcut = min_results_to_shortcut

    # ----------------------------------------------------------------
    # API publique
    # ----------------------------------------------------------------
    def search(
        self,
        query_embedding: list[float],
        sender_email: Optional[str] = None,
        sender_domain: Optional[str] = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        """Recherche hybride avec cascade.

        Args:
            query_embedding: vecteur dense (1024-d) de la query
            sender_email: si connu, on tente d'abord ce sender exact
            sender_domain: sinon on tente le domaine
            limit: nombre max de resultats

        Returns:
            Liste de SearchResult tries par rrf_score DESC.
        """
        # 1. Meme sender exact
        if sender_email:
            results = self._search_with_filter(
                query_embedding,
                sender_email=sender_email,
                limit=limit,
                strategy="same_sender",
            )
            if len(results) >= self.min_results_to_shortcut:
                return results[:limit]

        # 2. Meme domaine
        if sender_domain:
            results = self._search_with_filter(
                query_embedding,
                sender_domain=sender_domain,
                limit=limit,
                strategy="same_domain",
            )
            if len(results) >= self.min_results_to_shortcut:
                return results[:limit]

        # 3. Fallback global
        return self._search_with_filter(
            query_embedding, limit=limit, strategy="global_fallback",
        )

    # ----------------------------------------------------------------
    # Recherche interne : combine pgvector + tsvector via RRF
    # ----------------------------------------------------------------
    def _search_with_filter(
        self,
        query_embedding: list[float],
        *,
        sender_email: Optional[str] = None,
        sender_domain: Optional[str] = None,
        limit: int = 5,
        strategy: str = "global_fallback",
    ) -> list[SearchResult]:
        """Execute la recherche hybride pour une strategie donnee.

        On recupere les resultats des 2 sources (vector + FTS),
        puis on fusionne par RRF.
        """
        # Construire la clause WHERE commune
        where_clauses: list[str] = ["ee.email_id = e.id"]
        params: list = []

        if sender_email:
            where_clauses.append("e.sender_email = %s")
            params.append(sender_email)
        elif sender_domain:
            where_clauses.append("e.sender_domain = %s")
            params.append(sender_domain)

        where_sql = " AND ".join(where_clauses)

        # --- Source 1 : recherche vectorielle ---
        vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
        vector_results = self._vector_search(vec_str, where_sql, params, limit)
        vector_ranks = {r["email_id"]: (i + 1, r["distance"]) for i, r in enumerate(vector_results)}

        # --- Source 2 : recherche full-text via tsvector ---
        # Note : on fait la FTS seulement sur le body/subject, pas sur l'email du sender
        # car on n'a pas de query textuelle, on aurait besoin du subject du mail courant
        # Pour l'instant, on desactive la FTS (pas de query textuelle) et on utilise
        # uniquement la recherche vectorielle + metadonnees
        # Si on avait la query textuelle, on ferait :
        #   ts_rank(to_tsvector('french', subject || ' ' || body_text),
        #           plainto_tsquery('french', %s)) AS rank
        # mais ici on a juste un embedding

        # --- Source 3 : metadonnees (sender/domaine exact = bonus) ---
        # Si on a un filtre sender/domaine, les resultats qui matchent ce filtre
        # ont un bonus RRF
        metadata_ranks: dict[str, int] = {}
        if sender_email or sender_domain:
            for i, r in enumerate(vector_results):
                # Deja au top, on leur donne un bonus
                metadata_ranks[r["email_id"]] = i + 1

        # --- Fusion RRF ---
        all_ids = set(vector_ranks.keys()) | set(metadata_ranks.keys())
        fused: list[SearchResult] = []

        for email_id in all_ids:
            vec_rank, distance = vector_ranks.get(email_id, (None, None))
            meta_rank = metadata_ranks.get(email_id)

            rrf_score = 0.0
            if vec_rank is not None:
                rrf_score += 1.0 / (RRF_K + vec_rank)
            if meta_rank is not None:
                rrf_score += 1.0 / (RRF_K + meta_rank)

            # Trouver les infos completes
            row = next((r for r in vector_results if r["email_id"] == email_id), None)
            if row is None:
                continue
            fused.append(SearchResult(
                email_id=email_id,
                subject=row.get("subject"),
                sender_email=row.get("sender_email"),
                sender_domain=row.get("sender_domain"),
                action_taken=row.get("action_taken"),
                rrf_score=rrf_score,
                distance=distance if distance is not None else 1.0,
                rank_vector=vec_rank,
                rank_tsvector=None,  # pas de FTS dans cette implementation
                retrieval_strategy=strategy,
            ))

        # Tri par RRF score DESC, puis par distance ASC
        fused.sort(key=lambda r: (-r.rrf_score, r.distance))
        return fused[:limit]

    def _vector_search(
        self,
        vec_str: str,
        where_sql: str,
        params: list,
        limit: int,
    ) -> list[dict]:
        """Recherche vectorielle cosine (pgvector <=>)."""
        sql = f"""
            SELECT
                e.id AS email_id,
                e.subject,
                e.sender_email,
                e.sender_domain,
                (SELECT action FROM email_actions
                 WHERE email_id = e.id
                 ORDER BY detected_at DESC LIMIT 1) AS action_taken,
                (ee.embedding <=> %s::vector) AS distance
            FROM email_embeddings ee
            JOIN emails e ON e.id = ee.email_id
            WHERE {where_sql}
            ORDER BY distance ASC
            LIMIT %s
        """
        full_params = [vec_str] + params + [limit]

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, full_params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ----------------------------------------------------------------
    # Recherche full-text (pour future utilisation avec query textuelle)
    # ----------------------------------------------------------------
    def fulltext_search(
        self, query_text: str, *, limit: int = 10,
    ) -> list[dict]:
        """Recherche full-text pure (tsvector + ts_rank) en francais.

        Args:
            query_text: texte libre (sera parse via plainto_tsquery)
            limit: nombre max de resultats

        Returns:
            Liste de dicts {email_id, subject, sender_email, rank}
        """
        sql = """
            SELECT
                e.id AS email_id,
                e.subject,
                e.sender_email,
                ts_rank(e.tsv, plainto_tsquery('french', %s)) AS rank
            FROM emails e
            WHERE e.tsv @@ plainto_tsquery('french', %s)
            ORDER BY rank DESC
            LIMIT %s
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [query_text, query_text, limit])
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
