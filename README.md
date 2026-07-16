# Agent Mail 24/7

Agent autonome de gestion d'emails par IA locale, sans cloud, sans
suppression, sous garde-fous. 100% du traitement IA sur votre serveur.

> **Trois phases d'autonomie, mesurées avant d'être accordées :**
> P0 *observer* → P1 *proposer* → P2 *agir*.

## 🚀 Installation rapide

```bash
# 1. Cloner et installer
cd ~/email-learner
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configurer
cp configs/.env.example configs/.env
cp configs/config.yaml.example configs/config.yaml
# Éditer configs/config.yaml (IPs, paramètres)
# Éditer configs/.env (mot de passe DB, credentials Gmail)

# 3. Initialiser la base
alembic upgrade head

# 4. Lancer
python -m src.main
```

## 🏗️ Architecture

```
Gmail API ──► Observer ──► Parser (anti-injection) ──► Embedder
                  │              │                        │
                  ▼              ▼                        ▼
            sync_state     emails (PostgreSQL)   email_embeddings
                                                          │
Rules Engine ──► Recommender (P1) ──► Decider (P2) ──► Action Worker
                          │                │                 │
                          ▼                ▼                 ▼
                  decision_journal    action_queue     Gmail API
                          │                │                 │
                          └────► Dashboard FastAPI ◄──────────┘
```

**Stack :** Python 3.11+, PostgreSQL 15 + pgvector, Ollama (bge-m3 + LLM),
Firecracker (sandbox), FastAPI, Caddy.

## 📁 Structure du projet

```
agent-mail/
├── src/                    # Code source Python
│   ├── config.py           # Settings Pydantic (YAML + env)
│   ├── models.py           # Modèles Pydantic (MailDecision, EmailInDB, etc.)
│   ├── db.py               # Helper connexion psycopg2
│   ├── parser.py           # Parsing Gmail + sanitization nh3 + 7 anti-injection
│   ├── gmail_client.py     # Wrapper Gmail API avec allowlist stricte
│   ├── ingester.py         # UPSERT idempotent dans PostgreSQL
│   ├── embedder.py         # bge-m3 via Ollama + recherche vectorielle
│   ├── rules_engine.py     # 6 règles statiques + mots-clés critiques
│   └── main.py             # Point d'entrée daemon
├── tests/                  # 69 tests pytest (0 échec)
│   ├── test_config.py      # Settings Pydantic
│   ├── test_models.py      # Validation Pydantic stricte
│   ├── test_parser.py      # 7 cas adversariaux + intégration
│   ├── test_gmail_client.py # Allowlist + scan source
│   ├── test_rules_engine.py # Toutes les règles
│   └── test_embedder.py    # Construction texte embeddé
├── alembic/                # Migrations DB
│   └── versions/
│       └── 001_initial_schema.py  # 9 tables, 10 index, 1 trigger
├── configs/                # Configuration
│   ├── config.yaml.example
│   └── .env.example
├── static/                 # Cours HTML (héritage pédagogique)
│   ├── cours-agent-mail-24-7.html
│   ├── index.html / prompts.html / plan.html / outils.html
│   └── cover.png
├── audio/                  # Versions audio (Edge TTS)
├── docs/                   # Specs, prompts, plan
│   ├── SPEC-agent-mail-v5.md
│   ├── PROMPTS-agent-mail.md
│   ├── PLAN-TRAVAIL-agent-mail.md
│   └── OUTILS-agent-mail.md
├── systemd/                # (à venir) services daemon
├── scripts/                # (à venir) backup, restore, build VM
├── archive/                # Anciennes versions
├── versions/               # Snapshots antérieurs
├── screenshots/            # Captures d'écran du cours
├── requirements.txt
├── alembic.ini
├── pytest.ini
├── .gitignore
└── README.md (ce fichier)
```

## 🛠️ Commandes CLI

```bash
# Tests
python -m pytest tests/ -v

# Daemon
python -m src.main                  # boucle infinie
python -m src.main --sync-once      # une seule passe de sync
python -m src.main --embed 100      # embedde 100 emails
python -m src.main --health         # affiche l'état et sort
python -m src.main --debug          # logs DEBUG

# Migrations
alembic upgrade head                # applique toutes les migrations
alembic downgrade -1                # rollback d'une migration
alembic current                     # affiche la version courante
```

## 🔒 Sécurité (philosophie du projet)

**Toutes les protections sont obligatoires et vérifiables :**

| Couche | Implémentation | Test |
|--------|---------------|------|
| Anti-injection prompt | 7 patterns détectés par `parser.py` | `test_case_1_*.py` à `test_case_7_*.py` |
| Sandboxing | À venir : Firecracker VM jetable | — |
| Allowlist Gmail | 19 méthodes autorisées, 7 interdites | `test_*_forbidden` |
| Mots-clés critiques | JAMAIS auto-archivés (facture, paiement, etc.) | `test_rule_no_false_archive_for_billing` |
| Idempotence | `INSERT ... ON CONFLICT DO UPDATE` | `test_action_queue_item_idempotency_key_required` |
| Append-only journal | `decision_journal` n'a que des INSERTs | (à tester E2E) |
| Kill-switch P2 | `Settings.p2.enabled = false` par défaut | `test_settings_default_safety` |
| Pas de cloud | Ollama local, scope `gmail.modify` uniquement | (à valider au déploiement) |

## 🧪 Tests

```bash
# Tout
python -m pytest

# Avec couverture
python -m pytest --cov=src --cov-report=term-missing

# Tests adversariaux uniquement
python -m pytest -m adversarial -v

# Tests d'intégration (nécessitent PostgreSQL + Ollama)
python -m pytest -m integration -v
```

**État actuel : 69 tests, 100% passent, 0 dépendance réseau.**

## 📚 Documentation

- **Spec complète** : `docs/SPEC-agent-mail-v5.md` (15 sections)
- **Prompts par subagent** : `docs/PROMPTS-agent-mail.md` (14 agents)
- **Plan de travail** : `docs/PLAN-TRAVAIL-agent-mail.md`
- **Outils requis** : `docs/OUTILS-agent-mail.md`
- **Cours HTML interactif** : `static/cours-agent-mail-24-7.html`
- **Versions audio** : `audio/cours-agent-mail.mp3` (27 min)

## 🚦 État d'avancement

| Phase | Statut | Composants |
|-------|--------|------------|
| **P0 - Fondations** | 🟢 ~70% | config, models, db, parser, ingester, embedder, gmail_client, rules_engine |
| **P1 - Assistance** | 🟡 À faire | recommender, dashboard |
| **P2 - Autonomie** | 🔴 À faire | decider, action_worker, sandbox Firecracker |
| **DevOps** | 🟡 À faire | systemd, Caddy, backup, restore test |

## ⚖️ Licence & contact

Cours et code publiés par **explodev.fr**.

> Les adresses IP et noms de machines sont génériques
> (`serveur-local`, `10.0.0.XXX`). Aucune infrastructure réelle n'est exposée.
