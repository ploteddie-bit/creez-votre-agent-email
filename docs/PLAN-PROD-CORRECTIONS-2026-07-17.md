# PLAN DE TRAVAIL — Corrections Production

> **Projet :** Agent Mail 24/7
> **Date :** 2026-07-17
> **Base :** `SYNTHESE-COHERENCE-2026-07-17.md` (backlog consolidé, 15 correctifs dédupliqués)
> **Objectif :** passer le livrable de « MVP démontrable, tests verts » à « production-ready sur ia-general »
> **Règle d'or :** aucun correctif n'est « fait » tant que son test est vert ET que la suite complète (238 tests actuels + nouveaux) passe.

---

## 1. Critères de succès (nouvelle Definition of Done)

La DoD précédente était contournable par construction (greps de forme).
Celle-ci est fondée sur des **smoke tests réels** :

- [ ] `python -m src.main sync --max 1` ingère réellement 1 mail (token OAuth valide requis)
- [ ] `python -m src.main health` retourne du JSON avec `db_reachable: true`
- [ ] `pytest` : 0 fail, 0 skip non justifié
- [ ] P2 activé en cold start (0 mail en base) n'exécute **aucune** action automatique
- [ ] Après redémarrage du daemon : compteurs quota/rejets/domaines conservés
- [ ] Aucun `raise` permanent de type « not configured » dans le chemin nominal
- [ ] `git log` : un commit par phase, message conventional commit en français

---

## 2. Phase E — Déblocage production (P0) ~1 journée

> Sans cette phase, rien ne tourne : le pipeline d'entrée est mort et les
> garde-fous P2 sont poreux. **C'est le plan B minimal si interruption.**

### E1 — OAuth : vrai chargement des credentials 🔴

**Fichier :** `src/gmail_client.py:225-237`

- [ ] Implémenter `_load_credentials` : lecture `configs/token.json`,
      refresh automatique du access token expiré (`google-auth`),
      réécriture du token rafraîchi sur disque
- [ ] `cmd_setup_oauth` (`main.py:160`) : générer `token.json` via le flow
      OAuth installed-app (pas seulement un guide texte)
- [ ] Supprimer le `raise GmailAuthError` permanent du chemin nominal
      (le garder uniquement si token absent ET non rafraîchissable)

**Tests :**
- [ ] Mock `Credentials.from_authorized_user_file` → service construit
- [ ] Token expiré → `refresh()` appelé, token réécrit
- [ ] Token absent → erreur claire avec instruction `setup-oauth`

**Done :** `python -m src.main sync --max 1` ingère 1 mail réel.

**Dépendances :** credentials GCP (`configs/gmail-credentials.json`, gitignored).

---

### E2 — Gardes de volume P2 + cold start 🔴

**Fichier :** `src/decider.py:104-177` (`should_auto_execute`), `:243,246` (`get_window_precision`)

- [ ] Ajouter garde n°10 : `COUNT(*) FROM emails >= 2000` (SPEC §7.4)
- [ ] Ajouter garde n°11 : propositions P1 traitées >= 500
- [ ] Ajouter garde n°12 : aucun faux archivage critique sur les 100 dernières
- [ ] Cold start : `get_window_precision` retourne **0.0** (pas 1.0) quand pas
      de données → force P1 par défaut
- [ ] Requête `COUNT(*)` → remplacer par `EXISTS`/limite (perf, REVIEW §4.4)

**Tests :**
- [ ] 0 mail en base + P2 activé → `should_auto_execute` = False (toutes actions)
- [ ] 2000 mails + 500 propositions + précision OK → True
- [ ] Précision fenêtre sans données = 0.0

**Done :** le test « cold start n'exécute rien » est vert.

---

### E3 — SQL invalide du decider 🔴

**Fichier :** `src/decider.py:371-372`

- [ ] Remplacer `UPDATE … WHERE email_id = %s ORDER BY created_at DESC LIMIT 1`
      (invalide PostgreSQL) par une sous-requête sur `id` :

```sql
UPDATE decision_journal SET execution_status = 'pending'
WHERE id = (
    SELECT id FROM decision_journal
    WHERE email_id = %s ORDER BY created_at DESC LIMIT 1
)
```

- [ ] Scanner `src/` pour d'autres `UPDATE/DELETE … ORDER BY/LIMIT`

**Tests :**
- [ ] Test unitaire : vérifie la forme de la requête (regex/sous-requête sur id)
- [ ] Si PostgreSQL de test dispo : exécution réelle (sinon, ticket lint SQL)

**Done :** requête valide PostgreSQL, test vert.

---

### E4 — Circuit breaker persistant 🔴

**Fichier :** `src/main.py:214-257` (`_run_one_cycle`), `:260-309` (`cmd_daemon`)

- [ ] Instancier `GmailObserver`, `Embedder`, `ActionWorker` **une seule fois**
      dans `cmd_daemon` (avant la boucle), les passer à `_run_one_cycle`
- [ ] Le `CircuitBreaker` vit ainsi sur toute la durée du daemon (quota réel)
- [ ] Bonus : persister `quota_used_today` dans `sync_state` ou fichier JSON
      pour survivre au redémarrage (REVIEW §2.1/§3)

**Tests :**
- [ ] Le quota s'accumule entre 2 cycles simulés (pas de reset)
- [ ] Après « redémarrage » (rechargement état), quota conservé

**Done :** test d'accumulation quota vert.

---

### E5 — Commit Phase E

```
fix(prod): OAuth réel + gardes volume P2 + SQL decider + circuit breaker persistant
```

**Vérification avant commit :** `git status` (ne pas embarquer le travail
parallèle non lié — ex. modifs CSP actuellement non commitées dans
`src/config.py` / `configs/config.yaml.example`).

---

## 3. Phase F — Robustesse (P1) ~1 journée

### F1 — Persistance des états P2 🟠

- [ ] `decider.py:170` — compteur de rejets consécutifs → table dédiée
      (`decider_state`) ou colonne ; migration Alembic `003`
- [ ] `rules_engine.py:248-260` — `add/remove_low_priority_domain` réécrivent
      le JSON (ou migrent en table) ; mettre à jour `tests/test_sql_contracts.py`
      si nouvelle table
- [ ] `dashboard.py` PUT `/api/config` → persister (table `app_config`)

**Tests :** modification → « redémarrage » (nouvelle instance) → valeur conservée.

### F2 — Race condition historyId 🟠

**Fichier :** `src/observer.py:446-498` (`sync_delta`)

- [ ] Committer `last_history_id` après chaque batch ingéré (pas seulement en
      fin de sync) — l'UPSERT rend la redondance bénigne (PROD-READINESS §2)

**Tests :** crash simulé à mi-sync → reprise sans perte, doublons absorbés
par l'UPSERT.

### F3 — Parsing `.env` robuste 🟠

**Fichier :** `src/db.py:73-83`

- [ ] Remplacer le parsing manuel par `python-dotenv` (déjà dépendance
      transitive de pydantic-settings)

**Tests :** valeur contenant `=`, guillemets, espaces → parsing correct.

### F4 — Timeouts sur appels externes 🟠

- [ ] `recommender.py` / `embedder.py` : `httpx.Client(timeout=...)` explicite
      (+ client réutilisé, REVIEW §4.6)
- [ ] `gmail_client.py` : timeout sur `execute()` des appels API

**Tests :** appel simulé lent → TimeoutError levée et gérée (pas de hang).

### F5 — Rate limiting dashboard + sémaphore LLM 🟠

- [ ] `dashboard.py` : middleware rate limit (ex. slowapi ou compteur simple)
      sur les POST sensibles (`/approve`, `/reject`, `/config`, `/sync`)
- [ ] `recommender.py` : sémaphore bornant les appels LLM concurrents

**Tests :** rafale de requêtes → 429 ; N appels LLM simultanés ≤ borne.

### F6 — Commit Phase F

```
fix(prod): persistance états P2 + race historyId + dotenv + timeouts + rate limit
```

---

## 4. Phase G — Finition (P2) ~½-1 journée

| # | Tâche | Fichier | Effort |
|---|-------|---------|--------|
| G1 | `asyncio.get_event_loop()` → `get_running_loop()` + fallback | `recommender.py:407` | 15 min |
| G2 | `POST /api/sync` → tâche d'arrière-plan FastAPI (`BackgroundTasks`) | `dashboard.py` | 30 min |
| G3 | Arbitrage sandbox VM : décision oui/non documentée, 3 docs alignés | docs | 15 min |
| G4 | DevOps : units systemd, timer backup `pg_dump`, `restore_test.sh` | `systemd/`, `scripts/` | 1-2 h |
| G5 | Batch INSERT embeddings (perf N+1) | `embedder.py` | 30 min |
| G6 | Client HTTP réutilisé (si pas fait en F4) | `recommender.py` | 15 min |

### Commit Phase G

```
chore(prod): finitions (asyncio, sync async, devops, perfs)
```

---

## 5. Validation finale (smoke tests réels)

- [ ] **S1** : `python -m src.main setup-oauth` puis `sync --max 1` → 1 mail en base
- [ ] **S2** : `python -m src.main daemon` 10 min → cycles propres, quota
      circuit breaker visible dans `health`
- [ ] **S3** : P2 ON en cold start → 0 action auto ; après seed 2000 mails +
      500 propositions → auto-exécution conforme aux seuils
- [ ] **S4** : kill -9 à mi-sync → redémarrage → pas de perte, pas de doublon
- [ ] **S5** : `pytest` complet → 0 fail ; grep placeholders → 0
- [ ] **S6** : `./scripts/restore_test.sh` → restauration OK

---

## 6. Récapitulatif

| Phase | Contenu | Effort | Blocage levé |
|-------|---------|--------|--------------|
| E | OAuth + gardes P2 + SQL + circuit breaker | ~1 j | Le pipeline tourne, P2 sécurisé |
| F | Persistance + race + dotenv + timeouts + rate limit | ~1 j | Survit aux crashs et à la charge |
| G | Finitions + devops | ~½-1 j | Exploitable au quotidien |
| **Total** | | **~2,5-3 j** | Cohérent avec l'estimation PROD-READINESS (2-3 j) |

---

## 7. Risques & mitigations

| Risque | Mitigation |
|--------|------------|
| Credentials GCP absents → E1 bloqué | Faire E2-E4 d'abord si besoin ; E1 nécessite la console Google Cloud |
| Travail parallèle (Mavis) sur les mêmes fichiers | `git log -3 && git status` avant chaque session ; commits séparés par phase |
| Tests mockés ne voient pas le SQL réel (bug E3-type) | Ajouter un ticket « test d'intégration PostgreSQL réel » en Phase G |
| Modifs non commitées actuelles (CSP config.py) | Les committer ou les stasher AVANT de démarrer Phase E |

---

## 8. Ordre recommandé si interruption (Plan B)

1. **E1 + E3** (pipeline tourne, pas de crash SQL)
2. **E2** (sécurité P2 — non négociable avant activation)
3. **E4** (anti-ban Gmail)
4. F1, F2 (survie aux crashs)
5. Le reste en post-production

---

*Plan créé le 2026-07-17. Sources : SYNTHESE-COHERENCE-2026-07-17.md,
PROD-READINESS.md, SPEC-agent-mail v5 §7.4/§8.2/§10.*
