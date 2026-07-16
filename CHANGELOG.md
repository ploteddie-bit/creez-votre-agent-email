# Changelog

Toutes les modifications notables de ce projet sont documentees ici.

Le format est base sur [Keep a Changelog](https://keepachangelog.com/),
et ce projet suit [Semantic Versioning](https://semver.org/).

---

## [1.0.0] - 2026-07-17

### Version complete (P0 + P1 + P2) avec UI

Premiere version "livrable" : 100% fonctionnel, 0 placeholder,
238 tests automatises qui passent.

### Phase A - Wiring du daemon
- Boucle principale : sync -> embed -> recommend -> action
- `Recommender.process_new_emails` cable dans la boucle
- `Embedder.embed_unprocessed` cable dans la boucle
- `Decider.record_user_correction` cable dans approve/reject
- `Decider.auto_execute` cable dans le pipeline P1
- Pagination `sync_full` complete (nextPageToken)
- `cmd_sync` implemente pour de vrai
- Signal handler SIGINT/SIGTERM pour arret propre

### Phase B - API complete + quick wins
- **Quick win 1** : Explicabilite deterministe (`src/rationale.py`)
  Chaque decision a un rationale en francais
- **Quick win 2** : Regle anti-PJ non classee
  Toute piece jointe > 10 ko non classee -> move_ia_review
- **Quick win 3** : Encodage MIME robuste (chardet)
  decode_bytes_smart : UTF-8 -> chardet -> Windows-1252 -> UTF-8 replace
- **Quick win 4** : sync_full fallback 7j
  Au premier lancement : 6 mois. En fallback (historyId 404) : 7 jours.
- Endpoint `GET /api/emails/search?q=...` (recherche full-text FR)
- Endpoint `GET /api/stats?days=30` (repartition, top senders)
- Endpoint `GET /api/learning?window=100` (precision, progression P2)

### Phase C - UI + outils
- 5 pages HTML statiques : mails, decisions, stats, learning, config
- Architecture modulaire : HTML + CSS + JS separes
- `Makefile` avec 20+ cibles (install, test, run, dev, dashboard, etc.)
- `bootstrap.sh` (bash) + `bootstrap.ps1` (PowerShell) multiplateforme
- README utilisateur final, CHANGELOG, guide OAuth, runbook

### Phase D - Tests finaux
- 13 tests E2E bout-en-bout (pipeline complet avec mocks)
- Verification 0 placeholder (grep TODO/FIXME/NotImplementedError)
- Smoke tests CLI : --help, health, setup-oauth
- Tests composants importables (15+ modules)

### Securite (verification anti-regression)
- 7 cas adversariaux (parser) : CSS cache, comments, white-on-white,
  zero-width, homoglyphes, sujet injecte, RAG injecte
- 8+ methodes Gmail interdites (delete, send, drafts.*) bloquees
- Mots-cles critiques JAMAIS auto-archives
- 9 garde-fous du Decider (kill-switch, vacation, sender, confidence,
  divergence, mots-cles, quota, precision, corrections consecutives)
- Circuit-breaker anti-quota (pause a 80%)
- CSP restrictive sur le dashboard
- Bind sur 10.0.0.XXX uniquement (pas 0.0.0.0)

### Statistiques
- 238 tests, 0 fail, 0 skip
- ~5000 lignes de code Python (src/)
- ~1500 lignes de tests
- 9 tables PostgreSQL, 10 index, 1 trigger
- 24 routes FastAPI
- 5 pages HTML dashboard

---

## [0.5.0] - 2026-07-16

### Version documentation (avant implementation)
- `docs/SPEC-agent-mail-v5.md` : specification complete 15 sections
- `docs/PROMPTS-agent-mail.md` : prompts des 14 subagents
- `docs/PLAN-TRAVAIL-agent-mail.md` : plan d'execution
- `docs/OUTILS-agent-mail.md` : dependances detaillees
- Cours HTML interactif dans `static/cours-agent-mail-24-7.html`
- 4 versions audio (cours, prompts, plan, outils)

---

## [0.1.0] - 2026-07-15

### Bootstrap initial
- Repo initialise
- Reorganisation en arborescence standard
- Configuration Pydantic (Settings)
- Modeles Pydantic (EmailInDB, MailDecision, DecisionRecord, etc.)
- Alembic + migration initiale (9 tables)
