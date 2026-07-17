# ANALYSE DE COHÉRENCE — Objectif ↔ Revue Qwen ↔ Code réel

> **Date :** 2026-07-17
> **Auteur :** Kimi (analyse en lecture seule, vérifiée contre le code)
> **Périmètre :** SPEC v5, PLAN-TRAVAIL, PLAN-LIVRABLE-FINAL, revue Qwen du 17/07/2026, état du code au commit `f03bc35`
> **Méthode :** chaque affirmation de la revue Qwen a été vérifiée par lecture directe du code et exécution de la suite de tests (238 passed, 0 fail)

---

## 1. L'objectif du logiciel (SPEC v5) — avis

L'objectif est **clair, bien découpé et défendable** : agent mail 100 % local en 3 phases progressives — **P0 enregistrer → P1 proposer → P2 décider** — avec une philosophie sécurité explicite (jamais de suppression, allowlist Gmail, anti-injection, journal append-only).

**Points forts du design :**

- **Prudence architecturale** : seuils P2 basés sur la *précision mesurée*, pas sur la confiance déclarée du LLM (SPEC §7.2). C'est la bonne approche.
- **Séparation classification / opération** (§6.5) : le LLM ne touche jamais l'API Gmail ; tout passe par un wrapper déterministe allowlisté. Le système est auditable.

**Points faibles de la SPEC elle-même :**

- OAuth §4.1 laissé vague (« récupère le credential dans kimi-rag ou sg-rag ») — ce flou s'est propagé jusqu'au code (voir divergence n°2).
- Sandbox VM mentionné en question ouverte (« pourquoi ne pas les ouvrir dans un sandbox sécurisé ? », §6.6) sans arbitrage — ce flou se retrouve dans la divergence n°3.

---

## 2. Verdict sur la revue Qwen : à lire avec un filtre

**Les deux tiers des « bugs bloquants » sont faux sur le code réel.** La signature de l'erreur est révélatrice : le §1.1 décrit des doubles underscores manquants (`future`, `name`, `file` au lieu de `__future__`, `__name__`, `__file__`) — c'est exactement ce qui arrive quand du code est copié-collé dans une interface de chat Markdown (les `__` deviennent du gras). Qwen a analysé une **version déformée par le copier-coller**, pas les vrais fichiers. Sa note de fin l'admet : *« il pourrait manquer des fichiers de tests, de migration, de configuration »*.

En revanche, **sa section §2 (angles morts architecturaux) est précieuse** : les points réels y sont concentrés.

### 2.1 Affirmations FAUSSES (artefacts d'analyse — ne rien corriger)

| § | Claim Qwen | Réalité vérifiée |
|---|-----------|------------------|
| 1.1 | Imports cassés dans les 19 fichiers | **Faux** — tous les fichiers ont `from __future__ import annotations` et `getLogger(__name__)` corrects ; 238 tests passent (un seul import cassé suffirait à tout faire échouer) |
| 1.2 | Allowlist Gmail avec espaces terminaux → tout rejeté | **Faux** — `src/gmail_client.py:58-85` propre, normalisation dans `validate_call`, 16 tests dédiés passent |
| 1.9 / 1.10 | Mots-clés et domaines critiques avec espaces → sécurité compromise | **Faux** — `src/rules_engine.py:42` et `:50` propres (`"facture", "paiement", "impôt"…`) |
| 1.5 / 1.6 / 2.2 / 2.9 | Espaces parasites (dashboard, action_worker, parser, prompt LLM) | **Faux** — 28 tests dashboard, 16 action_worker, 22 parser passent |
| 1.8 | Doc `main.py` vs parser CLI désalignés | **Faux** — `src/main.py:18` et `:353` alignés sur `setup-oauth` |
| 2.11 | Colonne `tsv` jamais peuplée → full-text toujours vide | **Faux** — trigger PostgreSQL présent : `alembic/versions/001_initial_schema.py:66-79` |

### 2.2 Affirmations VRAIES et confirmées (le vrai backlog)

| § | Claim | Preuve dans le code | Gravité |
|---|-------|---------------------|---------|
| 1.3 | OAuth est un stub mort | `src/gmail_client.py:234` — `_load_credentials` lève toujours `GmailAuthError`. Le système ne peut pas s'authentifier à Gmail | 🔴 Bloqueur production |
| 1.4 | SQL invalide dans decider | `src/decider.py:371-372` — `UPDATE … ORDER BY … LIMIT 1`, refusé par PostgreSQL. Invisible dans les tests car la DB est mockée | 🔴 Crash garanti en prod |
| 2.1 | Amnésie du circuit breaker | `src/main.py:228` — `GmailObserver()` recréé à chaque cycle → compteur de quota remis à zéro à chaque polling. Garde-fou anti-ban Gmail inopérant | 🔴 Risque bannissement API |
| 2.8 | Cold start = précision 100 % | `src/decider.py:243,246` — `return 1.0`. Combiné à l'absence des gardes de volume (divergence n°1), risque d'auto-exécution aveugle | 🔴 Sécurité P2 |
| 2.4 | Compteur de 3 rejets en mémoire | `src/decider.py:170` — `_consecutive_rejections` est un dict d'instance, perdu entre requêtes et redémarrages | 🟠 Garde-fou P2 |
| 2.13 | Domaines low-priority jamais persistés | `src/rules_engine.py:248-260` — `add`/`remove` modifient le set en mémoire sans réécrire le JSON | 🟠 Apprentissage perdu |
| 2.10 | `asyncio.get_event_loop()` déprécié | `src/recommender.py:407` — vrai, mineur (garde `is_running()` présente) | 🟡 Robustesse |
| 3.4 | Config PUT non persistée | Confirmé aussi par PLAN-LIVRABLE-FINAL §2.2 (« mémoire seule ») | 🟡 Connu |

---

## 3. État réel du code (au 2026-07-17)

- **12 commits** ; phases A → D toutes commitées (`feat(phase A/B/C)`, `test(phase D)`)
- **238 tests, 0 échec** ; 20 modules `src/` ; 13 pages statiques ; `Makefile` + `bootstrap.sh` présents
- **Structure conforme à SPEC §11 à ~90 %** : tous les modules cœur existent, schéma SQL sous contrat de test (`tests/test_sql_contracts.py`), pipeline complet câblé (sync → embed → recommend → decide → execute)

---

## 4. Cohérence entre objectif, revue et code — les 5 vraies divergences

### 🔴 Divergence n°1 (risque max) — Garde-fous P2 : SPEC vs code

La SPEC §7.4 exige pour activer P2 : **2000+ emails ingérés, 500+ propositions P1 traitées, aucun faux archivage critique**. `should_auto_execute` (`src/decider.py:104-177`) ne vérifie **aucun de ces volumes** — et la précision cold start retourne 1.0 (`decider.py:243`).

**Conséquence :** si le kill-switch P2 est activé au premier démarrage, le système peut auto-exécuter dès le premier mail d'un expéditeur « connu », sans aucun historique. La SPEC avait prévu exactement ce filet ; le code ne l'a pas implémenté.

### 🔴 Divergence n°2 — OAuth : SPEC floue → plan esquivé → code stub

La SPEC ne dit pas comment charger les credentials, le plan livrable a documenté (`SETUP-OAUTH.md`) plutôt qu'implémenté, et le code lève une erreur (`gmail_client.py:234`). Cohérent dans l'esprit « ne pas bloquer le livrable », mais **le système ne peut pas tourner en production** en l'état.

⚠️ Note : le critère DoD du plan (`grep raise.*Error.*--setup-oauth`) passe techniquement parce que le `raise` et le message sont sur deux lignes — le critère est satisfait à la lettre, pas à l'esprit.

### 🟡 Divergence n°3 — Sandbox VM : les docs se contredisent

PLAN-TRAVAIL en fait le subagent 3 (P0, testable isolément) ; la SPEC le laisse en question ouverte ; le plan livrable ne le mentionne pas ; le code ne contient ni `sandbox.py` ni `sandbox_vm.py`. Ce n'est pas un oubli de code, c'est un **arbitrage jamais tranché**. À décider explicitement.

### 🟡 Divergence n°4 — DevOps absent

Subagent 14 (systemd, Caddy, timers backup, `restore_test.sh`) : rien d'exécutable dans le repo, alors que SPEC §10 exige backups quotidiens + test de restauration. `RUNBOOK.md` existe en doc, mais aucun script.

### 🟢 Divergence n°5 (bénigne) — B1 implémenté autrement que prévu

Le plan disait « champ `decision_rationale` rempli pour chaque décision » (persisté, migration 002). Le code calcule le rationale à la volée dans l'API (`src/rationale.py` + `src/dashboard.py:779-808`), sans colonne DB. Fonctionnellement équivalent, arguably plus simple — mais différent de la lettre du plan.

### 🟢 Cohérences fortes à souligner

- Schéma DB conforme (trigger tsv, tables, index, contrat de test)
- Pipeline 10 étapes du recommender conforme à §7.3
- Les 9 garde-fous du decider correspondent à §8.2
- Allowlist Gmail conforme §3.3 (delete/send/drafts absents)
- Pages et endpoints §9.2/§9.3 tous présents

---

## 5. Tableau de synthèse

| Couple | Cohérence | Verdict |
|--------|-----------|---------|
| Objectif ↔ code | **Forte (~90 %)** | Architecture et philosophie respectées ; manquent les gardes de volume P2 et OAuth |
| Revue Qwen ↔ code | **Faible sur §1, forte sur §2** | Revue faite sur du code déformé par copier-coller ; ses « bugs bloquants » sont des mirages, ses « angles morts » sont réels et précieux |
| Plan livrable ↔ code | **Forte** | Phases A-D livrées ; seul B1 diffère (pour le mieux) ; DoD OAuth satisfait à la lettre seulement |

---

## 6. Backlog priorisé proposé (issu uniquement des bugs confirmés)

| # | Correctif | Fichiers | Priorité |
|---|-----------|----------|----------|
| 1 | Gardes de volume P2 (2000 mails / 500 propositions) + cold start → précision 0.0 | `src/decider.py` | 🔴 P0 |
| 2 | SQL decider : `UPDATE … ORDER BY … LIMIT` → sous-requête sur `id` | `src/decider.py:371` | 🔴 P0 |
| 3 | Circuit breaker : instance unique au démarrage du daemon (pas par cycle) | `src/main.py:228` | 🔴 P0 |
| 4 | Persistance des domaines low-priority (réécriture JSON ou table dédiée) | `src/rules_engine.py:248-260` | 🟠 P1 |
| 5 | Compteur de rejets consécutifs persisté en base | `src/decider.py` + `src/dashboard.py` | 🟠 P1 |
| 6 | OAuth : implémenter le vrai chargement `token.json` + refresh | `src/gmail_client.py:225` | 🟠 P1 (bloquant prod, mais gros chantier) |
| 7 | `asyncio.get_event_loop()` → `get_running_loop()` + fallback | `src/recommender.py:407` | 🟡 P2 |
| 8 | Arbitrage sandbox VM : décider oui/non et aligner les 3 docs | docs | 🟡 P2 |
| 9 | DevOps : systemd units, timer backup, `restore_test.sh` | nouveau `systemd/`, `scripts/` | 🟡 P2 |

**Limite connue de la suite de tests :** la DB étant mockée partout, une erreur de syntaxe SQL (comme le bug n°2) ne peut pas être détectée par pytest. Envisager à terme un test d'intégration sur PostgreSQL réel (ou un lint SQL) pour les requêtes critiques.

---

*Fin de l'analyse. Document généré en lecture seule — aucune modification de code n'a été faite pour le produire.*
