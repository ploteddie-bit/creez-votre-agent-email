# Analyse de cohérence — Objectif / Revues / Code réel

**Date :** 2026-07-17
**Type :** Analyse objective, lecture seule (aucun fichier modifié en dehors de celui-ci)
**Portée :** Comparaison entre la SPEC, les documents de planification/revue, et le code réellement présent dans `src/`.

---

## 1. Résumé exécutif

Le projet est **architecturalement sain et bien testé**, mais la revendication
« 100 % fonctionnel sans placeholder » est **vraie au sens littéral (grep) et
fausse au sens pratique**. Il existe un stub critique non marqué qui empêche le
pipeline Gmail de fonctionner. Les documents de revue sont globalement honnêtes,
mais **aucun des deux ne signale ce stub**, qui est pourtant le point le plus
bloquant.

| Niveau | Verdict |
|---|---|
| Conception (SPEC v5) | Sérieuse, cohérente, ambitieuse |
| Code réel | Maquette fonctionnelle très avancée (demo/MVP solide) |
| Revues | Honnêtes mais incomplètes |
| Plan de livrable | Vend du « 100 % fonctionnel » non atteint |

---

## 2. L'objectif du logiciel (SPEC v5)

Un agent autonome de gestion email en 3 phases :

- **P0** — Enregistrer tous les emails + actions utilisateur dans une base RAG
- **P1** — Proposer des actions basées sur l'historique (Few-Shot)
- **P2** — Décider de façon autonome avec journal append-only

Principes : IA 100 % locale, sécurité non négociable, **jamais de suppression**,
transparence totale, dashboard LAN 24/7.

---

## 3. Ce que disent les revues

| Document | Verdict | Honnêteté |
|---|---|---|
| `REVIEW-ANGLES-MORTS-2026-07-17.md` | Audit externe noté 7/10, 4 quick wins (~1h30) | Honnête |
| `PROD-READINESS.md` | **NON production-ready**, 3-5 jours de travail | Très honnête |

Les scores PROD-READINESS sont crédibles : Tests 9/10, Doc 9/10, mais
Robustness 5/10, Observability 3/10. Le document liste 5 critiques
(race condition historyId, pas de timeout, pas de rate limit, sémaphore LLM,
parsing `.env`) — **tout cela est réel**.

---

## 4. L'état réel du code (vérifié)

### Points confirmés positivement

- Zéro `pass`, `TODO`, `FIXME`, `NotImplementedError`, `placeholder` au sens grep ✅
- 14/14 endpoints FastAPI présents ✅
- Daemon loop fait du vrai travail (sync → embed → classify → actions) ✅
- Wiring complet : `Recommender.process_new_emails`,
  `Embedder.embed_unprocessed`, `Decider.record_user_correction` tous branchés ✅
- 5/5 pages statiques présentes (`mails`, `decisions`, `stats`, `learning`, `config`) ✅
- 238 tests collectés ✅
- `configs/config.yaml`, `Makefile`, `bootstrap.sh` présents ✅
- 21 modules Python, ~6 474 lignes (src/) ; ~2 800 lignes de tests

### ⚠️ Le point critique que grep ne voit pas

`src/gmail_client.py:_load_credentials` (lignes ~225-237) lève
**inconditionnellement** `GmailAuthError` :

```python
def _load_credentials(self, CredentialsClass):
    raise GmailAuthError(
        "OAuth credentials not configured. "
        "Run `python -m src.main --setup-oauth` first."
    )
```

Ce n'est **pas** un `NotImplementedError` ni un `pass` — c'est un `raise`
déguisé. Il passe donc tous les greps du « Definition of Done ».
Conséquence : **chaque appel Gmail API échoue à l'exécution**, donc tout le
pipeline d'ingestion (le cœur du P0) est non fonctionnel.

### Autres gaps réels confirmés

- `PUT /api/config` → mémoire seule (reconnu dans le code)
- `POST /api/sync` → synchrone/bloquant (bug réel)
- Compteurs en mémoire (`_consecutive_rejections`, quota circuit-breaker)
  → reset au redémarrage → sécurité P2 dégradée au reboot

---

## 5. Cohérence entre les 3 niveaux — l'analyse clé

Trois incohérences majeures.

### 🔴 Incohérence n°1 : le « Definition of Done » est contourné par construction

`PLAN-LIVRABLE-FINAL.md` §7 contient cette case :

> `grep -rE "raise.*Error.*run.*--setup-oauth" src/` retourne 0 résultat

Or le code contient **exactement** `raise GmailAuthError("...Run `python -m
src.main --setup-oauth` first.")`. Cette case du DoD n'est pas cochée, et
pourtant le « release 1.0.0 » a été fait. **Le critère de validation a été
défini puis ignoré.**

Le §2.1 du plan listait ce stub explicitement comme placeholder connu. Il a
survécu à toutes les phases A-B-C-D. C'est le signe que la définition
« 100 % fonctionnel = 0 grep hit » est **insuffisante par design** : elle ne
mesure que la forme, pas le fond.

### 🟠 Incohérence n°2 : les revues ne voient pas le trou principal

`PROD-READINESS.md` liste 5 critiques + 5 importantes — **aucune ne mentionne
`_load_credentials`**. Les deux revues passent à côté du fait que le pipeline
d'entrée est un stub dur. C'est paradoxal : la revue est honnête sur le fait
que ce n'est pas prod-ready, mais elle rate le défaut le plus simple (le système
ne peut pas récupérer un seul mail sans intervention manuelle). Cela suggère que
les revues ont été faites par lecture statique sans tenter un vrai
`python -m src.main sync`.

### 🟢 Cohérence réelle : l'architecture suit la SPEC

Sur le plan structurel, le code **respecte fidèlement** la SPEC :

- 9 tables PostgreSQL
- pgvector + tsvector français
- allowlist Gmail stricte
- anti-injection nh3
- cascade RRF (sender → domaine → global)
- Pydantic strict (`extra=forbid`)
- `decision_journal` append-only
- 9 garde-fous P2
- circuit-breaker par quota units

L'ossature est solide et cohérente avec la vision.

---

## 6. Avis objectif

**Sur la conception** : c'est un bon design de système personnel/LAN. Les choix
techniques (bge-m3, pgvector, Ollama local, allowlist stricte, soft-delete
IA-Review) sont défendables et cohérents avec l'objectif de souveraineté/privacy.
La SPEC est l'une des meilleures pièces du projet.

**Sur le code** : propre, lisible, testé (238 tests, coverage anti-injection et
allowlist). Le ratio ~6 500 lignes src / ~2 800 lignes tests est sain. C'est du
bon travail d'ingénierie sur le fond.

**Sur la revendication « 100 % fonctionnel »** : **non**, et c'est mesurable.
Trois raisons objectives :

1. Le pipeline Gmail ne peut pas fonctionner (`_load_credentials` lève systématiquement)
2. Le DoD du plan lui-même n'est pas satisfait (la case grep `--setup-oauth` reste non cochée)
3. Les compteurs P2 en mémoire rendent les garde-fous non persistants au redémarrage

**Sur les revues** : honnêtes et utiles, mais incomplètes. PROD-READINESS est le
meilleur document — il dit la vérité (« NON production-ready ») et ses 5
critiques tiennent debout. Le défaut est qu'aucune revue n'a fait un smoke test
réel (`python -m src.main sync --max 1` avec de vraies credentials), qui aurait
révélé le stub en 10 secondes.

**Sur la cohérence globale** : il y a un **décalage de registre** entre les trois
niveaux :

- La SPEC vise un système de production personnel sérieux
- Le code est une maquette fonctionnelle très avancée (demo/MVP solide)
- Les revues décrivent un « presque prêt pour client exigeant »
- Le plan de livrable vend du « 100 % fonctionnel »

Les quatre ne décrivent pas tout à fait le même objet. Le code réel correspond
le mieux à : **« MVP démontrable, tests verts, mais pipeline d'entrée Gmail non
câblé et garde-fous P2 non persistants »**.

---

## 7. Ce qu'il faudrait pour aligner tout ça

Pour que la revendication « 100 % fonctionnel » devienne vraie, par ordre de
blocage :

1. **Implémenter `_load_credentials`** (lire `configs/token.json`, refresh OAuth)
   — sans ça, rien ne tourne
2. **Cocher la case DoD §7** : s'assurer que le grep `--setup-oauth` retourne 0
   (le code ne doit plus contenir ce message d'erreur comme fallback permanent)
3. **Persister les compteurs Decider** (`decision_journal` ou table dédiée) pour
   que P2 survive au reboot
4. Les 5 critiques de `PROD-READINESS.md` (timeout, rate limit, sémaphore LLM,
   race historyId, parsing `.env`)

---

## 8. Méthodologie

Cette analyse a été réalisée en lecture seule :

- Lecture de la SPEC v5, du plan de travail, du plan de livrable final
- Lecture des deux documents de revue (`REVIEW-ANGLES-MORTS`, `PROD-READINESS`)
- Scan statique complet de `src/` (21 modules, ~6 474 lignes)
- Inventaire des endpoints, pages statiques, tests, configs
- Vérification du wiring (daemon loop, handlers approve/reject)
- Comparaison des configs (`config.yaml` vs `config.yaml.example`)

Aucun fichier source n'a été modifié. Seul ce rapport a été créé.
