# Handover agent-mail — 2026-07-17

Document de passation rédigé par Kimi (session du 2026-07-07 → 2026-07-17).
Toutes les valeurs ci-dessous ont été **vérifiées au moment de la rédaction**, pas supposées.

---

## 1. Objet du logiciel

Agent email personnel qui :
1. **Observe** la boîte Gmail (IMAP) et ingère les mails dans PostgreSQL ;
2. **Apprend** le style de l'utilisateur (embeddings bge-m3 via Ollama, RAG few-shot) ;
3. **Recommande** des réponses et **décide** des actions (priorité P2 + garde-fous) ;
4. Expose un **dashboard** (FastAPI + WebSocket) pour supervision et validation humaine.

Principe directeur : l'humain garde la main ; l'agent propose, ne s'exécute que dans un cadre strict (allowlist, circuit breaker, gardes volume).

---

## 2. Décisions d'architecture arrêtées (ne pas rouvrir)

| Décision | Détail | Référence |
|---|---|---|
| **Backend mail = IMAP uniquement** | OAuth / API Gmail **entièrement supprimé** (choix humain approuvé du 2026-07-17) : `gmail_client.py` et ses tests supprimés, deps `google-*` retirées, `setup-oauth` → `setup-imap` dans `main.py` | commit `f9224a8` |
| **Throttling Gmail respecté, jamais contourné** | Gmail limite les gros téléchargements (protection anti-fuite). Cadence < 1 msg/s ⇒ quota journalier épuisé ⇒ arrêt propre, reprise le lendemain | — |
| **Backfill par watermark UID** | Pas par offset (les nouveaux mails décalent les offsets). Watermark persisté dans `configs/sync_progress.json` (gitignoré) | `scripts/sync_backfill.py`, commit `e57b464` |
| **Secrets hors dépôt** | `.env` racine (GMAIL_ADDRESS + GMAIL_APP_PASSWORD) : **ne jamais lire, afficher, committer** | `.gitignore` |

---

## 3. État factuel vérifié (2026-07-17 ~07:00)

### Code & tests
- **290/290 tests verts** (`"C:/Python314/python.exe" -m pytest tests/ -q`, ~27 s).
- Tous les modules de la spec existent dans `src/` : `imap_client`, `ingester`, `embedder`, `observer`, `recommender`, `decider`, `action_worker`, `action_detector`, `rules_engine`, `rationale`, `dashboard`, `search`, `models`, `db`, `config`, `parser`, `attachment_parser`, `main`, `agent_mail_learning_state`.
- Dernier commit : **`e57b464`** — poussé sur `https://github.com/ploteddie-bit/creez-votre-agent-email.git` (branche `main`).

### Données
- PostgreSQL local : `127.0.0.1`, db `email_learner`, user `email_learner_app`, mot de passe via variable d'env `EMAIL_LEARNER_DB_PASSWORD`. Toutes les tables présentes.
- **6 579 emails en base** (49 pré-existants + 6 530 ingérés).
- Sync 6 mois : 11 010 mails éligibles (query `newer_than:6m -label:spam -label:promotions`) → **6 530 ingérés, 0 échec**. Arrêt volontaire à 59 % (throttle).
- **Watermark backfill = UID 8671** ; reste **~4 380 mails** (UID < 8671).
- `sync_state.last_history_id = 22931` → le daemon ingère les **nouveaux** mails en delta, sans retélécharger l'historique.

### Tâche planifiée nocturne (Kimi Automation)
- **ID : `automation_e17aff47-ff28-48c0-a2bc-e7696fd92f88`**
- Cron `12 3 * * *`, timezone **Europe/Paris** — chaque nuit à 03h12.
- Action : `"C:/Python314/python.exe" scripts/sync_backfill.py --limit 800 --workers 8` dans le workspace du projet.
- ~800 mails/nuit ⇒ **≈ 6 nuits** pour finir l'historique. Quand la sortie JSON indique `done=true` / `remaining_estimate=0` → l'historique est COMPLET et la tâche doit être **désactivée**.
- En cas d'échec d'un run : `Automation.readRunLogs` / `readRunTranscript` avec l'ID ci-dessus.

### ⚠️ Bloquant connu
- **Ollama est DOWN** (connexion refusée) ⇒ `embed` et `process` impossibles tant qu'il n'est pas démarré. Le sync IMAP n'en a pas besoin ; le backfill nocturne non plus.

---

## 4. ⚠️ Points de vigilance pour le repreneur

1. **Processus parallèle « Mavis »** : modifications **NON commitées** dans `src/config.py` (CSP DashboardSettings) et `configs/config.yaml.example`. **NE JAMAIS les committer.** Pour committer `config.py` partiellement, utiliser la technique des hunks séparés via `git apply --cached` avec un patch filtré. Toujours `git status --short` avant d'éditer.
2. **Ne jamais lire ni afficher `.env`** (ni `configs/.env`).
3. **Ne pas insister** quand Gmail throttle — c'est une protection, pas un bug.
4. Fichiers non suivis légitimes : `AGENTS.md`, les rapports `docs/*2026-07-17*.md`, `linkedin-kimi-k3.*` — à committer ou non selon le choix de l'utilisateur (pas encore décidé).
5. Push GitHub : utiliser `GCM_INTERACTIVE=never GIT_TERMINAL_PROMPT=0 git push origin main` pour éviter les prompts interactifs.

---

## 5. Travail restant (roadmap)

Références : `docs/PLAN-PROD-CORRECTIONS-2026-07-17.md` (plan de mise en production), `docs/REVIEW-ANGLES-MORTS-2026-07-17.md`, `docs/PLAN-LIVRABLE-FINAL.md`.

| Phase | Contenu | État |
|---|---|---|
| **Phase 2 — DB + Ingestion** | Alembic, `models.py`, `ingester.py` (UPSERT idempotent), `embedder.py` (bge-m3/Ollama) | Code + tests OK ; **embeddings réels en attente d'Ollama** |
| **Phase 3 — Décision** | `observer.py` (circuit breaker), `recommender.py` (RAG few-shot), `decider.py` (P2 + garde-fous), `action_worker.py` | Code + tests OK ; validation en conditions réelles à faire |
| **Phase 4 — Dashboard + DevOps** | FastAPI + WebSocket, tests adversariaux (7 obligatoires), systemd, Caddy, backup | Code dashboard présent ; durcissement prod à finir selon le plan |
| **Backfill historique** | ~4 380 mails restants | En cours — tâche nocturne (§3) |

### Amélioration envisagée (non engagée)
Ne télécharger que les parties texte via `BODYSTRUCTURE` (évite les pièces jointes ⇒ moins de quota IMAP). À étudier, pas implémenté.

---

## 6. Premières actions suggérées pour le repreneur

1. Vérifier le résultat du premier run nocturne du backfill (logs de l'Automation, ou `configs/sync_progress.json` : watermark doit avoir baissé sous 8671).
2. Démarrer Ollama, puis lancer `embed` / `process` sur les 6 530 mails déjà ingérés.
3. Relire `docs/PLAN-PROD-CORRECTIONS-2026-07-17.md` et reprendre la checklist production.
4. Trancher le sort des fichiers non suivis (committer la doc, ignorer ou ranger `linkedin-kimi-k3.*`).

---

## 7. Documents de référence (dans `docs/`)

- `SPEC-agent-mail-v5.md` — spécification de référence (`.backup.md` = version antérieure)
- `PLAN-LIVRABLE-FINAL.md` — plan de livraison
- `PLAN-PROD-CORRECTIONS-2026-07-17.md` — plan de corrections pour la production
- `SYNTHESE-COHERENCE-2026-07-17.md` — synthèse fusionnée des analyses de cohérence
- `RAPPORT-COHERENCE-2026-07-17_z.md` / `ANALYSE-COHERENCE-2026-07-17_ki.md` — analyses sources
- `REVIEW-ANGLES-MORTS-2026-07-17.md` — angles morts de la revue
- `RUNBOOK.md`, `SETUP-IMAP.md`, `PROD-READINESS.md`, `OUTILS-agent-mail.md`
