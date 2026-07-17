# AGENTS.md — Mémoire & instructions de projet (agent-mail)

> Lu automatiquement par ZCode à chaque session sur ce dépôt.
> Contient : conventions du projet + leçons méthodologiques acquises.

---

## 1. Le projet en bref

- **Nom :** agent-mail (SPEC-agent-mail v5)
- **Objectif :** agent mail 100 % local, 3 phases — P0 enregistrer → P1 proposer → P2 décider
- **Stack :** Python 3.11, FastAPI, PostgreSQL + pgvector, Ollama (bge-m3), Gmail API (scope `gmail.modify`)
- **Principes non négociables :** IA locale, jamais de suppression (soft-delete IA-Review),
  allowlist Gmail stricte, anti-injection nh3, journal `decision_journal` append-only
- **Conventions de code :** commentaires et docstrings **sans accents** dans `src/`
  (le projet a volontairement ASCII-only pour le source ; les accents sont dans `docs/` et les messages UI)

---

## 2. État connu du code (vérifié 2026-07-17)

- 238 tests verts, 0 fail — **mais la DB est mockée partout** (voir §3)
- Stub OAuth bloquant : `src/gmail_client.py:_load_credentials` lève systématiquement
- Bugs confirmés par revue croisée (Kimi) :
  - `decider.py:371` — SQL `UPDATE … ORDER BY … LIMIT 1` invalide en PostgreSQL
  - `main.py:228` — `GmailObserver()` recréé à chaque cycle → circuit breaker inopérant
  - `decider.py:243,246` — `get_window_precision` retourne 1.0 au cold start
  - `should_auto_execute` ne vérifie aucun volume (SPEC §7.4 exige 2000+ mails / 500+ props)
  - `rules_engine.py:248-260` — `add/remove_low_priority_domain` non persistés
  - Compteurs P2 (`_consecutive_rejections`, quota) en mémoire → reset au reboot

Backlog priorisé complet : voir `docs/ANALYSE-COHERENCE-2026-07-17_ki.md` §6.

---

## 3. Leçons méthodologiques (acquises sur ce projet)

### 3.1 `Explore` ≠ revue de code

L'agent `Explore` **localise** du code, il ne l'**audite** pas (c'est dans sa description).
Pour chasser les bugs de logique / contrats SQL / race conditions, utiliser :

- **`code-reviewer`** — bugs, logic errors, security, filtrage par confiance
- **`code-explorer`** — trace les chemins d'exécution (`cmd_daemon → _run_one_cycle → …`)

Bien combiner : 1 `Explore` (inventaire large) + 1 `code-reviewer` (fond) en parallèle,
plutôt que N × `Explore`.

### 3.2 « 238 tests verts » n'est pas une preuve de correction

La DB est mockée partout → une erreur de syntaxe SQL (ex. `UPDATE … ORDER BY … LIMIT`)
**ne peut pas être détectée par pytest**. Conséquence :

- Toujours se demander « ce test exerce-t-il vraiment le chemin critique ? »
- Pour les requêtes SQL critiques, prévoir un test d'intégration sur PostgreSQL réel,
  ou un lint SQL, en complément des tests unitaires mockés.

### 3.3 Ne pas affirmer « confirmé » sans exécuter

Quand la conclusion est « fonctionnel / non fonctionnel », **exécuter** plutôt que lire :

- `python -m src.main health` → vrai JSON healthcheck
- `python -m src.main sync --max 1` → révèle le stub OAuth en 10 s
- `pytest` réel plutôt que `--collect-only`

Skill applicable : `superpowers:verification-before-completion`
(*evidence before assertions always*).

### 3.4 Douter des « angles morts » des revues précédentes

`PROD-READINESS.md` liste 5 critiques + 5 importantes — **aucune ne mentionne le
stub OAuth**. Une revue par lecture statique rate les défauts qui ne se voient qu'à
l'exécution. Ne jamais prendre une revue existante pour exhaustive.

### 3.5 Dispatcher les agents par préoccupation, pas par redondance

Plutôt que 3 × `Explore` (même profil), dispatcher par type de travail :

- inventaire (Explore)
- bugs (code-reviewer)
- tracing (code-explorer)
- vérification (Bash direct ou skill de vérification)

Skill applicable : `superpowers:dispatching-parallel-agents`.

### 3.6 Le « Definition of Done » par grep est insuffisant par design

`grep -E "TODO|NotImplementedError"` ne voit pas un `raise GmailAuthError(...)`
déguisé en message utilisateur. Critère de forme ≠ critère de fond. Pour valider
« 100 % fonctionnel », ajouter un smoke test d'exécution, pas seulement un grep.

---

## 4. Documents de référence (ne pas répéter leur contenu ici)

- `docs/SPEC-agent-mail-v5.md` — SPEC de référence (structure, schéma DB, sécurité)
- `docs/PLAN-TRAVAIL-agent-mail.md` — découpage en 14 subagents
- `docs/PLAN-LIVRABLE-FINAL.md` — phases A-D et DoD
- `docs/REVIEW-ANGLES-MORTS-2026-07-17.md` — réponse à audit externe (4 quick wins)
- `docs/PROD-READINESS.md` — revue prod-readiness (5 critiques, scores par critère)
- `docs/ANALYSE-COHERENCE-2026-07-17_ki.md` — revue croisée la plus complète (backlog §6)

---

## 5. Préférences de l'utilisateur (Eddie)

- Langue : **français** pour tous les échanges
- Ne pas écraser de fichiers existants sans confirmation explicite
- Préfère les analyses objectives et falsifiables (claims vérifiables contre le code)
- Signaler sans détour quand une affirmation ne peut être vérifiée
