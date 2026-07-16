# Plan de mise en place — Livrable final 100% fonctionnel sans placeholder

**Date :** 2026-07-17
**Auteur :** Mavis (Mavis/MiMo)
**Objectif :** transformer le projet Agent Mail 24/7 en un livrable
**fonctionnel à 100%**, sans `pass`, sans `# TODO`, sans `NotImplementedError`,
sans `raise NotImplementedError`, sans `return # a implementer`.

---

## 1. Vision & critères de succès

### 1.1 Définition de "100% fonctionnel"

Un livrable est considéré 100% fonctionnel quand :

| Critère | Vérification |
|---------|--------------|
| Aucun `pass` dans du code de production | `grep -r "^\s*pass$" src/` |
| Aucun `# TODO` / `# FIXME` / `# a faire` | `grep -rE "TODO\|FIXME\|a faire" src/` |
| Aucun `raise NotImplementedError` | `grep -r "NotImplementedError" src/` |
| Tous les endpoints de la SPEC existent | routes FastAPI vs SPEC §9.3 |
| Toutes les pages statiques référencées existent | `static/*.html` vs routes |
| `python -m src.main` démarre le daemon | smoke test |
| `python -m src.main health` retourne du JSON valide | smoke test |
| `python -m src.main dashboard` sert le dashboard | smoke test |
| Tous les tests passent | `pytest` 0 fail, 0 skip |
| README utilisateur final explique install + run | doc |
| CHANGELOG documenté | doc |
| Les 5 quick wins de l'audit sont implémentés | tests + code |

### 1.2 Hors scope (à NE PAS faire pour ce livrable)

- Multi-compte Gmail (P3 dans SPEC)
- Privacy audit log dédié
- Intégration CalDAV
- Mode "Éco" / quantisation custom
- App mobile, notifications push
- Train d'un LLM custom (QLoRA fine-tune) — la SPEC le mentionne en P2+

Ces points sont documentés dans `REVIEW-ANGLES-MORTS-2026-07-17.md` comme
"hors scope v0" et restent en P3+.

---

## 2. Inventaire des placeholders & manques

État actuel, trouvé par scan statique du code.

### 2.1 Placeholders explicites

| # | Fichier | Ligne | Type | Description |
|---|---------|-------|------|-------------|
| 1 | `src/main.py` | 43 | docstring | `cmd_sync_once` marqué "placeholder — subagent 2" |
| 2 | `src/main.py` | ~70 | code | Boucle daemon = simple `time.sleep(1)`, pas de vraie orchestration |
| 3 | `src/gmail_client.py` | `_load_credentials` | code | `raise GmailAuthError("OAuth credentials not configured. Run python -m src.main --setup-oauth first.")` |

### 2.2 Endpoints manquants (SPEC §9.3)

| Endpoint | Statut | À faire |
|----------|--------|---------|
| `GET /api/health` | ✅ fait | - |
| `GET /api/emails` | ✅ fait | - |
| `GET /api/emails/{id}` | ✅ fait | - |
| `GET /api/emails/search` | ❌ manquant | À créer (recherche hybride) |
| `GET /api/decisions` | ✅ fait | - |
| `POST /api/decisions/{id}/approve` | ✅ fait (mais sans wiring Decider) | Câbler le Decider |
| `POST /api/decisions/{id}/reject` | ✅ fait (mais sans wiring Decider) | Câbler le Decider |
| `GET /api/stats` | ❌ manquant | À créer (répartition actions, top senders, etc.) |
| `GET /api/learning` | ❌ manquant | À créer (précision par action, progression P2) |
| `GET /api/config` | ✅ fait | - |
| `PUT /api/config` | ⚠️ fait mais mémoire seule | Persister ou documenter le scope |
| `POST /api/sync` | ✅ fait (mais cmd_sync_once stub) | Implémenter vraiment |
| `WS /api/ws` | ✅ fait | - |

### 2.3 Pages statiques manquantes

| Route | Page HTML | Statut |
|-------|-----------|--------|
| `/` | `index.html` | ✅ existe |
| `/mails` | `mails.html` | ❌ manquant (retombe sur redirect) |
| `/decisions` | `decisions.html` | ❌ manquant |
| `/stats` | `stats.html` | ❌ manquant |
| `/learning` | `learning.html` | ❌ manquant |
| `/config` | `config.html` | ❌ manquant |
| `/cours` | `cours-agent-mail-24-7.html` | ✅ existe |
| `/prompts` | (référencé dans la nav) | ❌ manquant |
| `/plan` | (référencé dans la nav) | ❌ manquant |
| `/outils` | `outils.html` | ✅ existe |

**Note :** les pages `prompts.html` et `plan.html` existent dans `static/`
mais ne sont pas routées dans le dashboard. À câbler.

### 2.4 Wiring manquant (code existe mais pas branché)

| Fonction | Statut | À faire |
|----------|--------|---------|
| `Decider.record_user_correction()` | existe, jamais appelé | Câbler dans `dashboard.approve_decision` / `reject_decision` |
| `Decider.auto_execute()` | existe, jamais appelé | Câbler dans le pipeline (Recommender + Decider) |
| `Recommender.process_new_emails()` | existe, jamais appelé | Câbler dans le daemon loop |
| `Observer.sync_full()` | partiellement | Finir la pagination `nextPageToken` |
| `Embedder.embed_unprocessed()` | existe, jamais appelé | Câbler dans le daemon loop |

### 2.5 Quick wins de l'audit (cf. REVIEW-ANGLES-MORTS-2026-07-17.md)

| Quick win | Statut | Priorité |
|-----------|--------|----------|
| Explicabilité déterministe | ❌ manquant | Haute |
| Règle anti-PJ non classées | ❌ manquant | Très haute (sécurité) |
| Encodage MIME robuste (chardet) | ❌ manquant | Haute |
| `sync_full` fallback 7j | ❌ manquant | Haute |

### 2.6 Configuration runtime manquante

| Fichier | Statut | À faire |
|---------|--------|---------|
| `configs/config.yaml` | ❌ manquant | Créer depuis `config.yaml.example` avec vraie IP |
| `configs/.env` | ❌ manquant | Créer depuis `.env.example` avec placeholders |
| Script de bootstrap (`make install` ou `bootstrap.sh`) | ❌ manquant | À créer |
| `Makefile` ou équivalent | ❌ manquant | À créer |

### 2.7 Documentation manquante

| Document | Statut | À faire |
|----------|--------|---------|
| README utilisateur final (install + run) | partiel (technique) | Réécrire pour utilisateur final |
| CHANGELOG | ❌ manquant | À créer |
| Runbook d'exploitation | ❌ manquant | À créer |
| Guide OAuth setup | ❌ manquant | À créer |
| Procédure de backup / restore | ❌ manquant | À créer |

---

## 3. Plan de bataille

### Phase A — Fondations finales (~1h)

**Objectif :** zéro placeholder dans le code, wiring complet.

| # | Tâche | Effort | Critère "Done" | Dépendances |
|---|-------|--------|----------------|-------------|
| A1 | Câbler `Recommender.process_new_emails` dans `main.daemon` | 10 min | Boucle daemon appelle le process, logge la progression | - |
| A2 | Câbler `Embedder.embed_unprocessed` dans `main.daemon` | 10 min | Idem | - |
| A3 | Câbler `Decider.record_user_correction` dans `dashboard.approve/reject` | 20 min | Approuver/rejeter une décision incrémente le compteur, désactive après 3 | - |
| A4 | Câbler `Decider.auto_execute` dans le pipeline P1 | 15 min | Si P2 activé et `should_auto_execute` OK, l'action est enqueue | A3 |
| A5 | Implémenter `Observer.sync_full` pagination complète | 20 min | Tous les `nextPageToken` sont consommés, ingestion jusqu'à `max_results` | - |
| A6 | Implémenter `cmd_sync_once` pour de vrai | 5 min | Lance un sync complet en une fois, logue le résultat | A5 |

**Livrable Phase A :** daemon qui tourne et fait la boucle ingestion →
embedding → recommandation → décision → exécution, sans crash.

### Phase B — API complète + Quick wins (~1h30)

**Objectif :** tous les endpoints de la SPEC + les 4 quick wins sécurité/UX.

| # | Tâche | Effort | Critère "Done" | Dépendances |
|---|-------|--------|----------------|-------------|
| B1 | Quick win 1 : explicabilité déterministe | 25 min | Champ `decision_rationale` rempli pour chaque décision, exposé dans l'API | - |
| B2 | Quick win 2 : règle anti-PJ non classées | 15 min | PJ non classée → move_ia_review, test vert | - |
| B3 | Quick win 3 : encodage MIME (chardet) | 20 min | Mail ISO-8859-1 ressort proprement, test vert | - |
| B4 | Quick win 4 : `sync_full` fallback 7j | 15 min | Fallback historyId 404 → sync sur 7 jours, pas 6 mois | - |
| B5 | Endpoint `GET /api/emails/search?q=...` | 20 min | Recherche full-text + sémantique retourne des résultats | - |
| B6 | Endpoint `GET /api/stats` | 20 min | Répartition actions, top senders, heatmap heures | - |
| B7 | Endpoint `GET /api/learning` | 20 min | Précision par action, progression P2, top domaines appris | - |
| B8 | Tests pour tous les nouveaux endpoints | 20 min | `pytest` 0 fail | B5-B7 |

**Livrable Phase B :** tous les endpoints de la SPEC répondent, les 4 quick wins sont en place et testés.

### Phase C — Pages UI + Configuration runtime (~45 min)

**Objectif :** l'utilisateur peut vraiment utiliser le produit.

| # | Tâche | Effort | Critère "Done" | Dépendances |
|---|-------|--------|----------------|-------------|
| C1 | Pages statiques minimales : `mails.html`, `decisions.html`, `stats.html`, `learning.html`, `config.html` | 30 min | Pages s'affichent, montrent les vraies données via fetch | B6-B7 |
| C2 | `Makefile` avec cibles `install`, `test`, `run`, `dev` | 10 min | `make test` lance pytest, `make run` lance le daemon | - |
| C3 | Script `bootstrap.sh` qui crée `.env` et `config.yaml` depuis les examples | 10 min | L'utilisateur fait `./bootstrap.sh` et c'est configuré | - |
| C4 | README final (orienté utilisateur, pas technique) | 15 min | Un non-tech peut suivre : install, config, run, debug | - |
| C5 | CHANGELOG (résumé des versions / évolutions) | 5 min | Fichier avec les entrées depuis le début | - |
| C6 | Guide OAuth setup (comment créer le projet GCP) | 10 min | Pas-à-pas avec screenshots texte | - |
| C7 | Procédure backup / restore (basée sur `pg_dump`) | 10 min | Document de 1 page, runnable | - |

**Livrable Phase C :** un utilisateur peut cloner le repo, suivre le README, et avoir un système fonctionnel.

### Phase D — Tests finaux + Smoke tests (~30 min)

| # | Tâche | Effort | Critère "Done" |
|---|-------|--------|----------------|
| D1 | Test E2E bout-en-bout (sans vraie DB, mocks) | 20 min | `pytest tests/test_e2e.py` 0 fail |
| D2 | Smoke test `python -m src.main health` retourne JSON | 2 min | OK |
| D3 | Smoke test `python -m src.main dashboard --port 8200` démarre | 3 min | OK sur le port spécifié |
| D4 | Vérification finale : `grep -rE "TODO\|FIXME\|NotImplementedError\|placeholder" src/` | 2 min | 0 résultat |
| D5 | `pytest --tb=short` complet | 3 min | 100% pass |

**Livrable Phase D :** le système est testé de bout en bout.

---

## 4. Récapitulatif effort

| Phase | Effort | Cumul |
|-------|--------|-------|
| A — Wiring & placeholders | 1h | 1h |
| B — API + quick wins | 1h30 | 2h30 |
| C — UI & configuration | 1h30 | 4h |
| D — Tests finaux | 30 min | 4h30 |
| **TOTAL** | **~4h30** | |

---

## 5. Risques & mitigations

| Risque | Impact | Mitigation |
|--------|--------|------------|
| Manque de temps (Eddie fatigue à 4h du mat') | Livrable incomplet | Plan B en §6 |
| Tests E2E révèlent des bugs profonds | Blocage | Marquer ces bugs en "post-livrable" plutôt que de tout refaire |
| OAuth setup trop complexe pour être documenté proprement | User bloqué à l'install | Garder la doc OAuth en v0.5, mettre un message d'erreur clair si credentials manquants |
| Performance de l'install (chaque page prend du temps) | User frustré | Tester l'install from scratch, mesurer le temps |

---

## 6. Plan B (si on n'a pas le temps)

Si on est interrompu avant la fin de la phase C, voici la **priorité de
sauvegarde** à appliquer dans l'ordre strict :

1. **Phase A complète** (sans elle, le daemon ne tourne pas)
2. **Quick wins 1-2-3-4** (sans eux, sécurité et UX dégradées)
3. **Endpoint `/api/stats` et `/api/learning`** (sans eux, le dashboard P2 est vide)
4. **Tests E2E + smoke tests** (sans eux, on n'est pas sûr que ça marche)
5. Phase C en dernier (nice to have)

Ce qui peut être reporté sans bloquer le livrable :
- Pages HTML statiques (le user peut les faire plus tard, ou ne pas en avoir)
- CHANGELOG / doc utilisateur (peut être faite en post-livrable)
- OAuth guide (peut être ajoutée en v0.5)

---

## 7. Validation finale (Definition of Done du livrable)

Le livrable est **officiellement terminé** quand :

- [ ] `grep -rE "TODO|FIXME|NotImplementedError|placeholder" src/` retourne 0 résultat
- [ ] `grep -rE "raise.*Error.*run.*--setup-oauth" src/` retourne 0 résultat
- [ ] `pytest` rapporte 0 fail, 0 skip
- [ ] `python -m src.main --help` montre toutes les sous-commandes (daemon, sync, embed, health, dashboard, setup-oauth)
- [ ] `python -m src.main health` retourne du JSON avec `db_reachable`, `ollama_reachable`, etc.
- [ ] Un utilisateur peut suivre le README et arriver à un système qui démarre
- [ ] `make test` et `make run` fonctionnent
- [ ] Le commit final a un message clair de type "release: 1.0.0"

---

## 8. Prochaine étape

**Immédiat :** démarrer la Phase A, tâche A1 (câblage du daemon).

Tu confirmes que je lance, ou tu veux ajuster le scope (ex: supprimer
certaines pages UI pour aller plus vite) ?
