# SYNTHÈSE DE COHÉRENCE — Objectif ↔ Revues ↔ Code réel

> **Date :** 2026-07-17
> **Nature :** Fusion de `ANALYSE-COHERENCE-2026-07-17.md` (Kimi) et de
> `RAPPORT-COHERENCE-2026-07-17_z.md`, complétée par les points de
> `PROD-READINESS.md`. En cas d'écart factuel entre les deux sources,
> les valeurs re-vérifiées dans le code prévalent.
> **Méthode :** lecture seule. Chaque affirmation a été vérifiée par lecture
> directe du code (`fichier:ligne`) et exécution de la suite de tests
> (238 passed, 0 fail).

---

## 1. Résumé exécutif

Le projet est **architecturalement sain, fidèle à sa SPEC et bien testé**, mais
la revendication « 100 % fonctionnel sans placeholder » est **vraie au sens
littéral (grep) et fausse au sens pratique** : un stub OAuth non marqué empêche
tout le pipeline d'entrée Gmail de fonctionner, et plusieurs garde-fous P2 sont
non persistants ou absents.

Il existe un **décalage de registre** entre les quatre niveaux du projet :

| Niveau | Ce qu'il décrit |
|--------|-----------------|
| SPEC v5 | Un système de production personnel sérieux |
| Code réel | Une maquette fonctionnelle très avancée (MVP démontrable, tests verts) |
| Revues | Un « presque prêt pour client exigeant » |
| Plan de livrable | Du « 100 % fonctionnel » |

Les quatre ne décrivent pas tout à fait le même objet.

| Aspect | Verdict |
|--------|---------|
| Conception (SPEC v5) | Sérieuse, cohérente, ambitieuse — l'une des meilleures pièces du projet |
| Code réel | Propre, lisible, testé (238/238) ; ossature fidèle à la SPEC |
| Revues | Honnêtes mais incomplètes (chacune a ses angles morts — voir §4) |
| Plan de livrable | DoD satisfait à la lettre, pas à l'esprit |

---

## 2. L'objectif du logiciel (SPEC v5) — avis

Agent mail 100 % local en 3 phases : **P0 enregistrer → P1 proposer → P2
décider**, avec une philosophie sécurité explicite (jamais de suppression,
allowlist Gmail, anti-injection, journal append-only, dashboard LAN 24/7).

**Points forts du design :**

- **Prudence architecturale** : seuils P2 basés sur la *précision mesurée*, pas
  sur la confiance déclarée du LLM (§7.2).
- **Séparation classification / opération** (§6.5) : le LLM ne touche jamais
  l'API Gmail ; tout passe par un wrapper déterministe allowlisté.
- Choix techniques défendables et cohérents avec l'objectif de souveraineté :
  bge-m3 multilingue, pgvector, Ollama local, soft-delete IA-Review.

**Points faibles de la SPEC elle-même :**

- OAuth §4.1 laissé vague (« récupère le credential dans kimi-rag ou sg-rag ») —
  ce flou s'est propagé jusqu'au code (divergence n°2).
- Sandbox VM mentionné en question ouverte (§6.6) sans arbitrage
  (divergence n°4).

---

## 3. État réel du code (vérifié au commit `f03bc35`)

- **238 tests, 0 échec** ; **20 modules** `src/` (**5 726 lignes**) ;
  **3 865 lignes** de tests (ratio tests/src ≈ 0,67)
- Phases A → D du plan livrable toutes commitées
- 14/14 endpoints FastAPI présents ; 13 pages statiques ; `Makefile` +
  `bootstrap.sh` présents
- Zéro `pass`, `TODO`, `FIXME`, `NotImplementedError`, `placeholder` au sens grep
- Wiring complet : daemon loop (sync → embed → classify → actions),
  `Recommender.process_new_emails`, `Embedder.embed_unprocessed`,
  `Decider.record_user_correction` branchés
- Schéma SQL sous contrat de test (`tests/test_sql_contracts.py`)
- **Aucun commit `release` n'existe** dans l'historique ; les cases du DoD §7 du
  plan livrable sont restées décochées (`- [ ]`)

---

## 4. Verdict sur les revues

### 4.1 Revue Qwen (`Qwen_markdown_20260717_em5ny16b2.md`) — à filtrer

**Les deux tiers de ses « bugs bloquants » (§1) sont faux sur le code réel.**
La signature de l'erreur est révélatrice : son §1.1 décrit des doubles
underscores manquants (`future`, `name`, `file`) — exactement ce qui arrive
quand du code est copié-collé dans une interface de chat Markdown (les `__`
deviennent du gras). Qwen a analysé une **version déformée par le
copier-coller**, pas les vrais fichiers.

**Affirmations FAUSSES — ne rien corriger :**

| § | Claim | Réalité vérifiée |
|---|-------|------------------|
| 1.1 | Imports cassés dans les 19 fichiers | Faux — `from __future__ import annotations` et `getLogger(__name__)` corrects partout ; 238 tests passent |
| 1.2 | Allowlist Gmail avec espaces → tout rejeté | Faux — `src/gmail_client.py:58-85` propre, 16 tests dédiés |
| 1.9 / 1.10 | Mots-clés et domaines critiques avec espaces | Faux — `src/rules_engine.py:42` et `:50` propres |
| 1.5 / 1.6 / 2.2 / 2.9 | Espaces parasites (dashboard, action_worker, parser, prompt LLM) | Faux — 28 tests dashboard, 16 action_worker, 22 parser passent |
| 1.8 | Doc `main.py` vs parser CLI désalignés | Faux — `src/main.py:18` et `:353` alignés sur `setup-oauth` |
| 2.11 | Colonne `tsv` jamais peuplée | Faux — trigger PostgreSQL présent : `alembic/versions/001_initial_schema.py:66-79` |

**Affirmations VRAIES et confirmées — le vrai trésor de cette revue :**

| § | Claim | Preuve |
|---|-------|--------|
| 1.3 | OAuth est un stub mort | `src/gmail_client.py:234` — `_load_credentials` lève toujours |
| 1.4 | SQL invalide dans decider | `src/decider.py:371-372` — `UPDATE … ORDER BY … LIMIT 1` |
| 2.1 | Amnésie du circuit breaker | `src/main.py:228` — `GmailObserver()` recréé à chaque cycle |
| 2.4 | Compteur de 3 rejets en mémoire | `src/decider.py:170` — dict d'instance |
| 2.8 | Cold start = précision 100 % | `src/decider.py:243,246` — `return 1.0` |
| 2.13 | Domaines low-priority non persistés | `src/rules_engine.py:248-260` |
| 2.10 | `asyncio.get_event_loop()` déprécié | `src/recommender.py:407` |
| 3.4 | Config PUT non persistée | Confirmé aussi par PLAN-LIVRABLE §2.2 |

### 4.2 `PROD-READINESS.md` — le plus honnête des documents

Verdict explicite : **NON production-ready**, effort estimé **2-3 jours** de
travail concentré. Scores crédibles : Tests 9/10, Documentation 9/10,
Maintenabilité 7/10, Sécurité 6/10, Robustesse 5/10, Observabilité 3/10.

Ses 5 critiques tiennent debout :

1. `db.py` — parsing `.env` fragile (valeurs contenant `=`, guillemets)
2. `observer.py` — race condition sur `last_history_id` (commit seulement en
   fin de sync → doublons/saturation quota après crash + 404)
3. Timeouts manquants sur les appels externes
4. Pas de rate limiting sur l'API dashboard
5. Pas de sémaphore sur les appels LLM

**Angle mort de ce document :** il ne signale pas le stub OAuth
(`_load_credentials`), pourtant le bloqueur le plus simple à détecter — ce qui
suggère une revue statique sans tentative de smoke test réel.

### 4.3 `RAPPORT-COHERENCE-2026-07-17_z.md` — bonne analyse méta, imprécisions factuelles

**Contributions précieuses (intégrées ici) :**

- Le cadrage « vrai au sens littéral (grep), faux au sens pratique »
- Le « décalage de registre » entre SPEC / code / revues / plan (§1)
- La critique du DoD : définir « fini = 0 grep hit » mesure la forme, pas le
  fond ; le stub OAuth est un `raise` déguisé, invisible aux critères choisis
- Le rappel méthodologique : aucune revue n'a fait de smoke test réel
  (`python -m src.main sync --max 1` aurait révélé le stub en 10 secondes)

**Imprécisions factuelles (corrigées dans cette synthèse) :**

| Affirmation du rapport | Réalité mesurée |
|------------------------|-----------------|
| « 21 modules Python » | 20 |
| « ~6 474 lignes src » | 5 726 |
| « ~2 800 lignes de tests » | 3 865 (le ratio est donc *meilleur* qu'annoncé) |
| « le release 1.0.0 a été fait » | Faux — 0 commit `release` dans l'historique |
| PROD-READINESS estime « 3-5 jours » | Le document dit « 2-3 jours » |
| « Aucun document de revue ne signale le stub OAuth » | Vrai pour REVIEW-ANGLES-MORTS et PROD-READINESS — mais `ANALYSE-COHERENCE-2026-07-17.md` le signalait déjà comme bloqueur n°1 |

**Angle mort du rapport :** il ne relève ni le SQL invalide du decider, ni le
cold start P2 à précision 1.0 combiné à l'absence des gardes de volume, et il
sous-estime l'amnésie du circuit breaker (reset **à chaque cycle**, pas
seulement au redémarrage).

---

## 5. Les 6 vraies divergences objectif ↔ code

### 🔴 Divergence n°1 — Le stub OAuth : SPEC floue → plan esquivé → code mort

`src/gmail_client.py:225-237` — `_load_credentials` lève **inconditionnellement**
`GmailAuthError`. Ce n'est ni un `pass`, ni un `TODO`, ni un
`NotImplementedError` : c'est un `raise` déguisé qui **passe tous les greps du
DoD**. Conséquence : chaque appel Gmail API échoue à l'exécution — tout le
pipeline d'ingestion (le cœur du P0) est non fonctionnel sans intervention
manuelle.

⚠️ Le critère DoD §7 (`grep "raise.*Error.*run.*--setup-oauth"`) est satisfait
à la lettre car le `raise` et le message sont sur des lignes différentes —
mais pas à l'esprit. Le plan §2.1 listait ce stub comme placeholder connu ; il
a survécu aux phases A-B-C-D.

### 🔴 Divergence n°2 — Garde-fous P2 : les volumes de la SPEC ne sont pas implémentés

La SPEC §7.4 exige pour activer P2 : **2000+ emails ingérés, 500+ propositions
P1 traitées, aucun faux archivage critique**. `should_auto_execute`
(`src/decider.py:104-177`) ne vérifie **aucun de ces volumes** — et la précision
cold start retourne 1.0 (`decider.py:243,246`).

**Conséquence :** le jour où OAuth fonctionne et que P2 est activé, le système
peut auto-exécuter dès le premier mail d'un expéditeur « connu », sans aucun
historique. C'est le scénario que la SPEC était censée empêcher.

### 🔴 Divergence n°3 — États critiques non persistants (amnésie au reboot ET à chaque cycle)

| État | Localisation | Conséquence |
|------|--------------|-------------|
| Compteur quota circuit breaker | `GmailObserver()` recréé à chaque cycle (`main.py:228`) | Garde-fou anti-ban Gmail quasiment toujours à zéro |
| Compteur de 3 rejets consécutifs | dict d'instance (`decider.py:170`) | Désactivation d'action perdue au reboot |
| Domaines low-priority appris | mémoire seule (`rules_engine.py:248-260`) | Apprentissage perdu au redémarrage |
| Config dashboard (PUT) | mémoire seule | Réglages perdus au redémarrage |

### 🟡 Divergence n°4 — Sandbox VM : arbitrage jamais tranché

PLAN-TRAVAIL en fait le subagent 3 (P0) ; la SPEC le laisse en question ouverte
; le plan livrable ne le mentionne pas ; le code ne contient ni `sandbox.py` ni
`sandbox_vm.py`. Ce n'est pas un oubli de code, c'est une **décision jamais
prise**. À trancher explicitement et aligner dans les 3 documents.

### 🟡 Divergence n°5 — DevOps absent

Subagent 14 (systemd, Caddy, timers backup, `restore_test.sh`) : rien
d'exécutable dans le repo, alors que SPEC §10 exige backups quotidiens + test
de restauration. `RUNBOOK.md` existe en doc seulement.

### 🟢 Divergence n°6 (bénigne) — B1 implémenté autrement que prévu

Le plan disait « champ `decision_rationale` rempli pour chaque décision »
(persisté, migration 002). Le code calcule le rationale à la volée dans l'API
(`src/rationale.py` + `src/dashboard.py:779-808`), sans colonne DB.
Fonctionnellement équivalent, arguably plus simple.

---

## 6. Cohérences fortes à souligner

- Architecture fidèle à SPEC §11 : tous les modules cœur présents
- Schéma DB conforme (9 tables, trigger tsv, index, contrat de test)
- Pipeline 10 étapes du recommender conforme à §7.3 (cascade sender → domaine
  → global, Few-Shot, Pydantic `extra=forbid`)
- Les 9 garde-fous du decider correspondent à §8.2
- Allowlist Gmail conforme §3.3 (delete/send/drafts absents)
- Anti-injection nh3 + tests adversariaux présents
- Pages et endpoints §9.2/§9.3 tous présents

---

## 7. Backlog consolidé et dédupliqué

Fusion des 3 sources (analyse Kimi, rapport `_z`, PROD-READINESS), par ordre de
blocage réel :

| # | Correctif | Fichiers | Source(s) | Priorité |
|---|-----------|----------|-----------|----------|
| 1 | **Implémenter `_load_credentials`** (lecture `token.json` + refresh auto) — sans ça, rien ne tourne | `src/gmail_client.py:225` | Kimi, `_z` | 🔴 P0 |
| 2 | **Gardes de volume P2** (2000 mails / 500 propositions) + cold start → précision 0.0 | `src/decider.py` | Kimi | 🔴 P0 |
| 3 | **SQL decider** : `UPDATE … ORDER BY … LIMIT` → sous-requête sur `id` | `src/decider.py:371` | Kimi, Qwen | 🔴 P0 |
| 4 | **Circuit breaker persistant** : instance unique au démarrage du daemon | `src/main.py:228` | Kimi, Qwen | 🔴 P0 |
| 5 | Persister compteur de rejets + domaines low-priority (table ou JSON réécrit) | `src/decider.py`, `src/rules_engine.py` | Kimi, `_z`, Qwen | 🟠 P1 |
| 6 | Race condition `last_history_id` : commit après chaque batch, pas en fin de sync | `src/observer.py` | PROD-READINESS | 🟠 P1 |
| 7 | Parsing `.env` → `python-dotenv` / pydantic-settings | `src/db.py` | PROD-READINESS | 🟠 P1 |
| 8 | Timeouts sur appels externes (Gmail, Ollama) | `gmail_client.py`, `embedder.py`, `recommender.py` | PROD-READINESS | 🟠 P1 |
| 9 | Rate limiting API dashboard + sémaphore appels LLM | `dashboard.py`, `recommender.py` | PROD-READINESS | 🟠 P1 |
| 10 | Refaire le DoD : critères fondés sur un **smoke test réel** (`sync --max 1`, `health`) et non sur des greps | `PLAN-LIVRABLE-FINAL.md` §7 | `_z` | 🟠 P1 |
| 11 | Persister la config dashboard (PUT) | `src/dashboard.py` | Kimi, `_z` | 🟡 P2 |
| 12 | `asyncio.get_event_loop()` → `get_running_loop()` + fallback | `src/recommender.py:407` | Kimi, Qwen | 🟡 P2 |
| 13 | Trancher l'arbitrage sandbox VM et aligner les 3 docs | docs | Kimi | 🟡 P2 |
| 14 | DevOps : systemd units, timer backup, `restore_test.sh` | `systemd/`, `scripts/` | Kimi, PLAN-TRAVAIL | 🟡 P2 |
| 15 | `POST /api/sync` synchrone → tâche d'arrière-plan FastAPI | `src/dashboard.py` | `_z`, Qwen | 🟡 P2 |

**Définition of Done proposée pour la prochaine itération** (remplace les greps) :

- [ ] `python -m src.main sync --max 1` ingère réellement 1 mail (avec token OAuth valide)
- [ ] `python -m src.main health` retourne du JSON avec `db_reachable: true`
- [ ] `pytest` 0 fail, 0 skip
- [ ] P2 activé en cold start (0 mail) n'exécute **aucune** action automatique
- [ ] Redémarrage du daemon : compteurs quota/rejets conservés
- [ ] Aucun `raise` permanent de type « not configured » dans le chemin nominal

**Limite connue de la suite de tests :** la DB étant mockée partout, une erreur
de syntaxe SQL (comme le bug n°3) est indétectable par pytest. Envisager un
test d'intégration sur PostgreSQL réel (ou un lint SQL) pour les requêtes
critiques.

---

## 8. Convergence des analyses

Trois analyses indépendantes écrites le même jour aboutissent au même constat
central : **stub OAuth + états P2 non persistants + écart forme/fond du DoD**.
Cette convergence est le signal le plus fiable que ce backlog est le bon.
L'analyse `_z` répond mieux à « pourquoi croyait-on que c'était fini ? » ;
l'analyse Kimi répond mieux à « qu'est-ce qui va casser quand ça tournera ? » ;
PROD-READINESS répond mieux à « que manque-t-il pour un client exigeant ? ».
Cette synthèse fusionne les trois réponses.

---

*Document produit en lecture seule — aucun fichier source modifié. Les deux
rapports sources (`ANALYSE-COHERENCE-2026-07-17.md`,
`RAPPORT-COHERENCE-2026-07-17_z.md`) sont conservés intacts.*
