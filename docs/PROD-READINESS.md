# Revue de code production — 2026-07-17

**Date :** 2026-07-17 vers 01h40
**Auteur :** Mavis (Mavis/MiMo)
**Contexte :** revue de code du livrable 1.0.0 en mode "production-ready" (sans
s'appuyer sur les tests comme garde-fou), comme si on livrait a un
client payant.

**Verdict global :** NON production-ready tel quel. Le code est
propre du point de vue tests (238/238) mais il manque des pieces
critiques pour un deploiement en production (timeouts, rate limiting,
metriques, fix de race conditions).

**Effort estime pour passer a 100% prod-ready :** 2-3 jours de travail
concentre par un dev senior.

---

## Tableau de bord

| Critere | Note | Commentaire |
|---------|------|-------------|
| Tests automatises | 9/10 | 238/238, bonne couverture (anti-injection, allowlist, contrats SQL) |
| Securite | 6/10 | Allowlist OK, anti-injection OK, mais pas de rate limit, pas d'audit deps |
| Robustesse | 5/10 | Bugs de race conditions, timeouts manquants, swallowing d'erreurs |
| Observabilite | 3/10 | Logs basiques, pas de metriques, pas de correlation_id |
| Documentation | 9/10 | README, guides, CHANGELOG, RUNBOOK complets |
| Maintenabilite | 7/10 | Code lisible, mais magic numbers, pas de pre-commit |
| **Production-ready ?** | **NON** | **Manque rate limit, timeouts, metriques, fix de 1-2 race conditions** |

---

## CRITIQUE (a corriger AVANT production)

### 1. `db.py:_read_env_var` — parsing .env fragile

**Fichier :** `src/db.py`, ligne ~73-83
**Code :**
```python
k, v = line.split("=", 1)   # plante sur KEY="value=with=equals"
```

**Probleme :**
- Si la valeur contient `=`, le parsing est casse
- Pas de gestion des guillemets (valeurs avec espaces, caracteres speciaux)
- Pas de gestion des multi-lignes
- Pas d'escape pour les guillemets imbriques

**Risque :** crash au boot si le `.env` contient une URL avec des
parametres (cas typique : `DATABASE_URL=postgresql://...?sslmode=require`).

**Action :** remplacer par `python-dotenv` (deja installe comme
dependance transitive de pydantic-settings), ou utiliser directement
le loader de pydantic-settings.

---

### 2. `observer.py:sync_delta` — race condition sur historyId

**Fichier :** `src/observer.py`, lignes ~480-510
**Scenario de panne :**

1. `sync_delta` demarre
2. `_ingest_one` reussit pour 50 mails
3. **Le daemon crash** (OOM, kill, segfault)
4. Au redemarrage, `sync_state.last_history_id` est l'**ancien** (pas
   mis a jour)
5. Gmail a **supprime** l'historique entre les 2 syncs (> 7 jours)
6. On tombe sur un 404 → `HistoryExpired` → sync_full fallback 7j
7. **Resultat :** saturation du quota Gmail + doublons

**Risque :** perte de mails, saturation du quota, boucle d'erreurs.

**Action :** commiter la position du `last_history_id` **apres chaque
batch d'ingestion**, pas seulement a la fin du sync_delta. Accepter
un peu de redondance (mails rejoues, mais c'est idempotent grace a
l'UPSERT).

---

### 3. `action_worker.py` — pas de timeout sur les appels Gmail

**Fichier :** `src/action_worker.py`, methode `_execute_action`
**Code :**
```python
self.gmail_client.modify_labels(email_id, add=..., remove=...)
```

Aucun timeout explicite. Si Gmail hang (reseau lent, rate limit non
detecte, etc.), le worker reste **bloque indefiniment**. Le daemon
ne progresse plus.

**Risque :** **blocage prod garanti** des qu'un appel Gmail depasse
le timeout implicite (qui n'est pas defini, donc c'est le timeout
par defaut de la lib Google = 5 min par appel).

**Action :** wrap chaque appel dans un timeout explicite (genre 30s
avec retry + backoff). Utiliser `asyncio.wait_for` ou un thread avec
timeout.

---

### 4. `recommender.py` — pas de semaphore sur les appels Ollama

**Fichier :** `src/recommender.py`, methode `_call_llm`
**Scenario :**

1. Le daemon `process_new_emails` est appele avec batch_size=50
2. Pour chaque email, on appelle `Recommender.recommend()`
3. Chaque appel fait un POST Ollama qui prend 5-30s
4. Si plusieurs batchs tournent en parallele (ou si le daemon est
   appele plusieurs fois rapidement), on peut avoir N appels Ollama
   simultanes
5. Ollama sature : un LLM llama3.1:8b prend ~6 Go RAM
6. **OOM kill** du processus Ollama → tous les appels suivants
   echouent

**Risque :** auto-DoS du LLM local. Le systeme s'effondre de
l'interieur des qu'il y a un peu de charge.

**Action :** `asyncio.Semaphore(2)` pour limiter a 2 LLM calls
concurrents. Le reste attend en queue.

---

### 5. `dashboard.py` — pas de rate limiting

**Fichier :** `src/dashboard.py`, FastAPI app
**Scenario :**

1. Bind sur LAN (10.0.0.XXX) — OK par design (SPEC §3.4)
2. Un user LAN compromis (ou un script qui boucle) flood
   `/api/decisions` ou `/api/sync`
3. Chaque requete fait une query SQL sur la DB
4. **Saturation de PostgreSQL** : CPU 100%, connexions epuisees
5. Tout le daemon devient lent

**Risque :** deni de service local. Pas de leak vers l'exterieur,
mais impact sur les autres services.

**Action :** ajouter `slowapi` ou middleware maison de rate limit
(10 req/s par IP, 100 req/min par IP). 30 minutes de travail.

---

## IMPORTANT (a corriger sous 1-2 sprints)

### 6. Pas de structured logging

**Constat :**
- Format actuel : `"%(asctime)s [%(levelname)s] %(name)s: %(message)s"`
- Pas de `correlation_id` → impossible de suivre une requete de bout
  en bout dans les logs
- Pas de contexte (user_id, email_id, decision_id) attache aux
  messages
- Pas de niveaux differencies par module

**Risque :** debugging en production = galere. Quand un user
rapporte "j'ai eu un probleme a 14h32", on ne peut pas retrouver le
contexte.

**Action :** `structlog` (deja dans requirements.txt !), ajouter un
`RequestIDMiddleware` qui genere un UUID par requete et le propage
dans tous les logs.

---

### 7. Pas de metriques Prometheus

**Constat :**
- Pas de compteur "mails processed", "decisions approved",
  "P2 actions executed"
- Pas d'histogramme de latence (sync, embed, recommend, action)
- Pas de gauge pour la taille de la queue
- Le `/api/health` est un debut, mais pas exploitable pour de
  l'alerting

**Risque :** pas de visibilite operationnelle. On ne sait pas si le
deamon est lent, si la queue grossit, si le quota Gmail est proche.

**Action :** `prometheus-client` + endpoint `/metrics` (format
standard). Grafana + alertes sur les seuils critiques.

---

### 8. Pas de validation des invariants dans `config.py`

**Fichier :** `src/config.py`
**Constat :** si on met `precision_thresholds.archive = 1.5` dans
`config.yaml`, ca passe. Idem pour des seuils `> 1` ou `< 0`.

**Risque :** configuration invalide qui passe inapercu jusqu'a
l'execution (division par zero, comparaison bizarre).

**Action :** Pydantic `Field(ge=0, le=1)` sur tous les seuils.
Ajouter un test qui verifie qu'une config invalide leve une erreur
au boot.

---

### 9. Pas de rotation de logs

**Constat :** on logge en stdout (systemd journald en prod). Pas
de max-size, pas de retention, pas de compression.

**Risque :** disk plein en long-running (apres plusieurs mois).

**Action :** `logrotate` config ou `journald.conf` avec
`SystemMaxUse=500M` + `MaxRetentionSec=1month`.

---

### 10. ~25 `except Exception` swallow

**Fichier :** partout (`src/*.py`)
**Constat :** la plupart loggent correctement, mais certains
retournent silencieusement des valeurs par defaut.

**Risque :** masquer des bugs intermittents. Un `except Exception:
pass` peut cacher un KeyboardInterrupt ou un SystemExit (oups).

**Action :** auditer les 25 occurrences, specifier le type
d'exception attendue (`except (ValueError, KeyError) as e:` au lieu
de `except Exception`), et logger avec stack trace (`exc_info=True`).

---

## NICE-TO-HAVE (refactoring)

### 11. SQL queries construites par f-string

**Fichier :** `src/dashboard.py`, lignes ~248-262
**Code :**
```python
where_sql = ("WHERE " + " AND ".join(where)) if where else ""
sql = f"SELECT ... {where_sql} LIMIT %s OFFSET %s"
```

**Statut :** SAFE (les inputs sont parametres via `params`, donc
psycopg2 fait l'escape). Mais c'est moche, un auditeur de secu va
le flagger.

**Action :** utiliser un query builder (`pypika`, `sqlalchemy` core)
ou migrer vers un ORM (SQLAlchemy declaratif). Travail significatif.

---

### 12. Pas de pre-commit hook

**Constat :** `.gitignore` est bon, mais pas de
`.pre-commit-config.yaml` avec black/ruff/mypy. Les erreurs de
style ne sont catchees qu'au moment de la CI (ou jamais).

**Action :** `pip install pre-commit && pre-commit install`.
Configuration : black, ruff, mypy, trailing-whitespace, end-of-file.
10 minutes de setup.

---

### 13. Pas de constantes nommees

**Constat :** `2000`, `100`, `50`, `30`, `5` partout dans
`observer`, `action_worker`, `dashboard`. Pas de
`MAX_PAGE_SIZE = 10000` explicite.

**Risque :** un dev met `max_results=1000000` en pensant bien faire
→ RAM explosee. Un autre met `retries=10000` → boucle infinie.

**Action :** extraire les magic numbers en constantes
`MAX_PAGE_SIZE = 10000`, `RETRY_BACKOFF_SECONDS = 5`,
`PAGINATION_DEFAULT = 50`, etc. dans un module `src/constants.py`.

---

### 14. Pas de type hints coherents partout

**Constat :** parfois `dict`, parfois `dict[str, Any]`. Pas
d'`Optional[]` explicite sur certains retours.

**Action :** passer mypy en strict sur le nouveau code, en mode
"incremental" sur l'existant. C'est du nettoyage.

---

### 15. Pas de health check approfondi

**Constat :** `/api/health` checke DB, Ollama, Gmail observer. Mais
il ne dit pas :
- L'age du dernier sync reussi
- La taille de la queue d'actions
- L'age du dernier embed
- Le quota Gmail utilise / restant

**Action :** enrichir le snapshot de l'observer et exposer dans
`/api/health` pour de l'alerting Prometheus/Grafana.

---

## Statistiques du code

| Metrique | Valeur |
|----------|--------|
| Modules Python | 20 (src/*.py) |
| Lignes de code | ~5 500 (src/) |
| Lignes de test | ~2 800 (tests/) |
| Fichiers HTML dashboard | 10 (static/*.html) |
| Fichiers JS dashboard | 6 (static/js/*.js) |
| Fichiers CSS | 1 (static/css/dashboard.css) |
| Tables PostgreSQL | 9 |
| Routes FastAPI | 24 |
| Tests pytest | 238 (0 fail, 0 skip) |
| Commits git | 25 (depuis 22h30) |

---

## Effort de remediation

| Section | Effort | Qui |
|---------|-------|-----|
| 5 critiques (1-5) | 1-2 jours | 1 dev senior |
| 5 importantes (6-10) | 1 jour | 1 dev senior |
| 5 nice-to-have (11-15) | 1-2 jours | 1 dev |
| **Total prod-ready** | **3-5 jours** | 1 dev senior |

---

## Conclusion

Le livrable est **techniquement complet et fonctionnel** du point
de vue des tests (238/238). Il est **pret pour un deploiement
interne / demo / MVP**. Il n'est **PAS pret pour un client
exigeant** sans les 5 corrections critiques et les 5
ameliorations importantes.

**Recommandation :** traiter les 5 critiques en priorite, puis
faire un audit de securite externe avant d'ouvrir l'acces aux
utilisateurs finaux.

---

**Signataire :** Mavis (Mavis/MiMo)
**Date :** 2026-07-17
**Commit de reference :** 687e762 (nettoyage post-Phase C)
