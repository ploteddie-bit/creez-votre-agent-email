# PLAN DE TRAVAIL — Agent Mail 24/7

> Orchestration par subagents — qui fait quoi, dans quel ordre, avec quelles dépendances  
> Projet : email-learner  
> Spec : SPEC-agent-mail v6  
> Créé : 2026-07-06

---

## Subagents

| # | Agent | Rôle | Phase | Dépendances |
|---|-------|------|-------|-------------|
| 1 | `db-architect` | PostgreSQL : user, schéma, migrations, indexes, triggers | P0 | Aucune |
| 2 | `gmail-sync` | OAuth, observer, historyId, sync delta, circuit-breaker | P0 | `db-architect` (tables) |
| 3 | `sandbox-vm` | Firecracker VM + conteneur Ollama `--network=none` + pool | P0 | Aucune (testable isolément) |
| 4 | `parser-sanitizer` | Parsing MIME, nh3, extraction PDF, détection actions | P0 | `gmail-sync` (emails bruts) |
| 5 | `embedder` | bge-m3 via Ollama, pgvector, index IVFFlat | P0 | `db-architect` + `parser-sanitizer` |
| 6 | `rules-engine` | Règles statiques, KNOWN_LOW_PRIORITY_DOMAINS auto | P0 | `db-architect` |
| 7 | `dashboard-core` | FastAPI, Caddy HTTPS, `/api/health`, pages statiques | P0 | Aucune (sert des pages vides au début) |
| 8 | `recommender` | P1 : cascade RRF, Few-Shot, Pydantic, confiance hybride | P1 | `embedder` + `rules-engine` |
| 9 | `dashboard-p1` | Pages décisions, Approuver/Rejeter, recherche hybride | P1 | `dashboard-core` + `recommender` |
| 10 | `action-worker` | Queue idempotente → Gmail API (modify labels) | P2 | `gmail-sync` + `db-architect` |
| 11 | `decider` | P2 autonome : seuils par action, garde-fous, mode vacances | P2 | `recommender` + `action-worker` |
| 12 | `dashboard-p2` | Stats, learning, config, kill-switch | P2 | `dashboard-p1` + `decider` |
| 13 | `tester` | Tests unitaires, adversariaux, E2E | Toutes | Tous les agents (exécuté après chaque) |
| 14 | `devops` | systemd, Caddy, backups, restore test, runbook | Toutes | Tous les agents (final) |

---

## 1. db-architect

**Rôle** : Créer la base de données PostgreSQL et toutes les tables.

**Tâches** :
- Créer l'utilisateur `email_learner_app` (non-superuser, hostssl)
- Créer la base `email_learner`
- Installer `postgresql-contrib` (dictionnaire français)
- Exécuter le schéma complet : 9 tables, 2 triggers, 10 index
- Générer la migration Alembic initiale
- Écrire `src/config.py` (connexion DB)

**Livrables** :
- `alembic/versions/001_initial_schema.py`
- `src/config.py`
- Base connectable : `psql -h 10.0.0.xxx -U email_learner_app -d email_learner`

**Validation** :
```sql
SELECT table_name FROM information_schema.tables WHERE table_schema='public';
-- Doit lister : emails, email_actions, email_embeddings, sync_state,
--               action_queue, gmail_labels, decision_journal,
--               learning_metrics, sandbox_alerts
SELECT to_tsvector('french', 'échéance facture');
-- Doit retourner un tsvector
```

---

## 2. gmail-sync

**Rôle** : Connecter Gmail API, récupérer les emails, gérer le delta.

**Tâches** :
- Écrire `src/gmail_client.py` (wrapper avec allowlist, interdictions delete/send)
- Écrire `src/observer.py` (polling history.list + circuit-breaker)
- Implémenter OAuth2 (scope `gmail.modify`)
- Sync initiale : `messages.list(q='newer_than:6m')`
- Sync delta : `history.list(startHistoryId=...)`
- Fallback full-resync sur 404 historyId
- Gestion codes erreur (429, 401, 403, 500, 503)
- Stocker `gmail_labels` (pas de hardcode)

**Livrables** :
- `src/gmail_client.py`
- `src/observer.py`
- `configs/gmail-credentials.json` (gitignored)
- `configs/token.json` (gitignored, généré)

**Validation** :
```bash
python -m src.main --sync-once --max 10
# → 10 emails dans la table emails
psql -c "SELECT COUNT(*) FROM emails;"
```

---

## 3. sandbox-vm

**Rôle** : Micro-VM Firecracker jetable avec conteneur Ollama isolé.

**Tâches** :
- Écrire `scripts/build_vm_image.sh` (Alpine + Python + conteneur Ollama)
- Écrire `src/sandbox.py` (orchestrateur : pré-filtre + pool VMs)
- Écrire `src/sandbox_vm.py` (agent dans la VM : prompt → Ollama → validation)
- Pool de 3-5 VMs pré-chauffées
- Détection anomalies : timeout, JSON invalide, appels système suspects
- Fallback Docker `--network=none` si pas de KVM
- Table `sandbox_alerts` + WebSocket push au dashboard

**Livrables** :
- `scripts/build_vm_image.sh`
- `src/sandbox.py`
- `src/sandbox_vm.py`
- `/opt/email-learner/vm/vmlinux.bin`
- `/opt/email-learner/vm/rootfs.ext4`

**Validation** :
```bash
./scripts/build_vm_image.sh
python -c "from src.sandbox import VMPool; p=VMPool(); vm=p.acquire(); r=vm.classify('test'); p.release(vm); print(r)"
# → JSON valide avec classification "unknown"
```

---

## 4. parser-sanitizer

**Rôle** : Transformer un email brut Gmail en données propres et structurées.

**Tâches** :
- Écrire `src/parser.py` : headers, nh3 HTML→texte, body_snippet
- Écrire `src/attachment_parser.py` : extraction PDF via pypdf
- Écrire `src/ingester.py` : UPSERT idempotent dans PostgreSQL
- Écrire `src/action_detector.py` : détection actions par delta labels
- Tests adversariaux (CSS caché, Unicode invisible, etc.)

**Livrables** :
- `src/parser.py`
- `src/attachment_parser.py`
- `src/ingester.py`
- `src/action_detector.py`

**Validation** :
```bash
python -m pytest tests/test_anti_injection.py -v
# → Tous les tests passent
python -m pytest tests/test_ingester.py -v
# → UPSERT idempotent vérifié
```

---

## 5. embedder

**Rôle** : Générer les embeddings bge-m3 et les stocker dans pgvector.

**Tâches** :
- Écrire `src/embedder.py` (batch processing via Ollama)
- Contenu de l'embedding : subject + body_snippet + sender_email + sender_domain + attachment_text
- Index IVFFlat cosine (100 listes)
- Retry si Ollama timeout
- Queue d'embedding (emails sans embedding)

**Livrables** :
- `src/embedder.py`

**Validation** :
```bash
python -m src.main --embed 100
psql -c "SELECT COUNT(*) FROM email_embeddings;"
# → 100
psql -c "SELECT vector_dims(embedding) FROM email_embeddings LIMIT 1;"
# → 1024
```

---

## 6. rules-engine

**Rôle** : Règles statiques de classification (cold start + fallback).

**Tâches** :
- Écrire `src/rules_engine.py` : 6 règles + CRITICAL_KEYWORDS
- Système d'apprentissage automatique de `KNOWN_LOW_PRIORITY_DOMAINS`
- Recalcul nocturne (cron ou scheduler interne)
- Retrait immédiat si comportement utilisateur change

**Livrables** :
- `src/rules_engine.py`

**Validation** :
```bash
python -m pytest tests/test_rules_engine.py -v
# → Chaque règle testée avec cas positifs et négatifs
```

---

## 7. dashboard-core

**Rôle** : Dashboard HTTPS minimal (santé système, pages vides).

**Tâches** :
- Écrire `src/dashboard.py` (FastAPI, bind 10.0.0.xxx:0000)
- Écrire `src/health.py` (endpoints + checks)
- Configurer Caddy (HTTPS local sur :8080 → :8000)
- Pages HTML statiques (vides, remplies par dashboard-p1/p2)
- WebSocket (connexion simple, sans auth)
- CSP restrictif
- Pas d'authentification

**Livrables** :
- `src/dashboard.py`
- `src/health.py`
- `static/index.html` (minimal, santé système)
- `static/style.css`
- `static/app.js` (WebSocket reconnect)
- Configuration Caddy

**Validation** :
```bash
curl -k https://10.0.0.xxx:8080/api/health | python3 -m json.tool
# → JSON avec tous les champs health
```

---

## 8. recommender

**Rôle** : Classification P1 — Few-Shot RAG dynamique.

**Tâches** :
- Écrire `src/search.py` (recherche hybride RRF)
- Écrire `src/recommender.py` : cascade sender→domaine→global
- Prompt Few-Shot sécurisé (§6.6 de la spec)
- Appel Ollama avec `format=MailDecision.model_json_schema()`
- Validation Pydantic `extra=forbid`
- Confiance hybride (dashboard uniquement, pas décisionnel)
- Détection divergence LLM/heuristique → forcer P1

**Livrables** :
- `src/search.py`
- `src/recommender.py`

**Validation** :
```bash
python -m pytest tests/test_recommender.py -v
# → Classification correcte sur jeu de test
```

---

## 9. dashboard-p1

**Rôle** : Interface P1 complète (décisions, recherche).

**Tâches** :
- Pages `/decisions` : tableau + boutons Approuver/Rejeter
- Page `/mails` : recherche hybride (full-text + sémantique)
- Page `/config` : sliders, toggles
- Intégration WebSocket pour mise à jour temps réel
- Échappement HTML systématique des contenus mail

**Livrables** :
- `static/decisions.html`
- `static/mails.html`
- `static/config.html`
- Mise à jour `src/dashboard.py` (endpoints P1)

**Validation** :
```bash
curl -k https://10.0.0.xxx:8080/api/emails/search?q=facture
# → Résultats pertinents
```

---

## 10. action-worker

**Rôle** : Exécuter les actions Gmail API via la queue idempotente.

**Tâches** :
- Écrire `src/action_worker.py` : consommer action_queue
- Idempotence : clé unique, pas de double exécution
- Actions autorisées : mark_read, archive, star, move_ia_review
- Interdictions vérifiées : pas de delete, send, transfer
- Retry avec backoff, max 3 tentatives
- Service systemd dédié

**Livrables** :
- `src/action_worker.py`
- `systemd/email-learner-worker.service`

**Validation** :
```bash
python -m pytest tests/test_action_worker.py -v
# → Queue consommée, idempotence vérifiée
python -m pytest tests/test_gmail_client.py -v
# → Aucune méthode interdite appelée
```

---

## 11. decider

**Rôle** : Décisions autonomes P2 avec garde-fous.

**Tâches** :
- Écrire `src/decider.py`
- Fenêtre glissante 100 dernières décisions
- Seuils par action : archive≥95%, mark_read≥90%, star≥85%, move_ia_review≥80%
- Garde-fous : jamais auto-archive si mots-clés critiques
- Divergence LLM/heuristique>0.3 → forcer P1
- Kill-switch dashboard → repasse P1
- Mode Vacances : P2 désactivé
- Quota quotidien max 20 actions/jour

**Livrables** :
- `src/decider.py`

**Validation** :
```bash
python -m pytest tests/test_decider.py -v  # (à créer par tester)
# → Seuils respectés, kill-switch fonctionnel
```

---

## 12. dashboard-p2

**Rôle** : Pages statistiques et apprentissage.

**Tâches** :
- Page `/stats` : camembert, barres, top expéditeurs, heatmap
- Page `/learning` : courbes précision, progression P2
- Graphiques Chart.js
- Kill-switch P2 + Mode Vacances (visuel)
- Export JSON complet

**Livrables** :
- `static/stats.html`
- `static/learning.html`
- Mise à jour `src/dashboard.py` (endpoints P2)

**Validation** :
```bash
curl -k https://10.0.0.xxx:8080/api/stats | python3 -m json.tool
# → Données agrégées
```

---

## 13. tester

**Rôle** : Tests unitaires, adversariaux, E2E pour tous les agents.

**Tâches** :
- `test_observer.py` : mock Gmail API
- `test_sandbox.py` : injection, prompt cache, timeout, escape VM
- `test_ingester.py` : UPSERT idempotent
- `test_rules_engine.py` : règles statiques
- `test_recommender.py` : classification P1
- `test_gmail_client.py` : interdictions delete/send
- `test_anti_injection.py` : 7 cas adversariaux
- `test_action_worker.py` : queue idempotente
- `test_e2e.py` : mock Gmail → ingestion → embedding → décision → dashboard

**Livrables** :
- 9 fichiers de test

**Validation** :
```bash
python -m pytest tests/ -v --cov=src --cov-report=term
# → 100% tests passent, coverage ≥ 80%
```

---

## 14. devops

**Rôle** : Déploiement, services, backups, monitoring.

**Tâches** :
- Services systemd : `email-learner.service`, `email-learner-worker.service`
- Timers : `email-learner-backup.timer` (pg_dump quotidien), `email-learner-train.timer`
- Script `restore_test.sh` (test restauration backup automatisé)
- Configuration Caddy (reverse proxy HTTPS)
- Vérifications pré-déploiement (liste section 9 de OUTILS-agent-mail.md)

**Livrables** :
- `systemd/email-learner.service`
- `systemd/email-learner-worker.service`
- `systemd/email-learner-backup.timer`
- `systemd/email-learner-train.timer`
- `scripts/restore_test.sh`

**Validation** :
```bash
sudo systemctl start email-learner
sudo systemctl status email-learner
# → active (running)
./scripts/restore_test.sh
# → "Restauration OK : 1850 rows (attendu ≥ 1831)"
```

---

## Ordre d'exécution

```
P0 ─────────────────────────────────────────────────────────────────────
│
├─ 1. db-architect        → tables, indexes, migrations
├─ 2. dashboard-core      → santé système visible pendant le reste
├─ 3. gmail-sync          → emails dans la base
├─ 4. parser-sanitizer    → nettoyage + actions
├─ 5. sandbox-vm          → VM Firecracker prête
├─ 6. embedder            → vecteurs bge-m3
├─ 7. rules-engine        → règles statiques
│
├─ 13. tester             → tests P0 (exécuté après chaque agent)
│
P1 ─────────────────────────────────────────────────────────────────────
│
├─ 8. recommender         → classification Few-Shot
├─ 9. dashboard-p1        → interface Approuver/Rejeter
│
├─ 13. tester             → tests P1
│
P2 ─────────────────────────────────────────────────────────────────────
│
├─ 10. action-worker      → queue → Gmail API
├─ 11. decider            → autonomie + garde-fous
├─ 12. dashboard-p2       → stats + learning
│
├─ 13. tester             → tests P2 + E2E
│
FINAL ───────────────────────────────────────────────────────────────────
│
└─ 14. devops             → systemd, Caddy, backups, runbook
```

---

## Règles de collaboration entre agents

1. **Chaque agent travaille dans son périmètre.** Ne pas toucher aux fichiers d'un autre agent sans nécessité explicite.
2. **Les dépendances sont strictes.** Un agent ne peut pas commencer tant que ses dépendances ne sont pas validées.
3. **Un agent = un prompt.** Le prompt contient : la spec de l'agent, ses dépendances, les livrables attendus, les critères de validation.
4. **Validation après chaque agent.** Le `tester` exécute les tests spécifiques à l'agent avant de passer au suivant.
5. **Communication via la base de données.** Les agents ne partagent pas de fichiers intermédiaires. Tout passe par PostgreSQL.
