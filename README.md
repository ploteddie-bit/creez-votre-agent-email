# Agent Mail 24/7

> **Un agent IA local qui gere votre boite mail sans cloud, sans
> suppression, sous votre controle total.**

Il apprend de vos habitudes, propose des actions, et (quand vous le
decidez) agit en autonomie avec des garde-fous stricts. Tout reste
sur votre serveur.

---

## En 30 secondes

```bash
# 1. Installer (une seule fois)
./bootstrap.sh

# 2. Configurer l'accès Gmail (IMAP, mot de passe d'application)
python -m src.main setup-imap
# Suivre les instructions a l'ecran

# 3. Lancer
make run
```

C'est tout. Le daemon tourne en boucle, le dashboard est sur
`http://10.0.0.XXX:8080` (derriere Caddy), et vous pouvez dormir
tranquille.

---

## Ce que ca fait

| Phase | Comportement | Par defaut |
|-------|--------------|------------|
| **P0** | Observe : enregistre chaque mail et chaque action, ne touche a rien | Active des le demarrage |
| **P1** | Propose : pour chaque mail, suggere une action que vous validez | Active des le demarrage |
| **P2** | Agit : execute seul si la precision mesuree est suffisante | **OFF** (kill-switch) |

**Trois principes non negociables :**

1. **Vos mails ne quittent jamais votre serveur.** Tout traitement
   IA est local (Ollama + bge-m3 + LLM local).
2. **Aucune suppression.** L'IA utilise uniquement le dossier
   `IA-Review` (soft-delete) pour signaler les mails a revoir.
3. **Append-only.** Le journal des decisions ne perd jamais rien :
   vous pouvez toujours rejouer l'historique.

---

## Installation pas-a-pas

### Prerequis

- **Python 3.11+**
- **PostgreSQL 15+** avec extension `pgvector`
- **Ollama** avec les modeles `bge-m3` (embeddings) et un LLM
  (par defaut `llama3.1:8b`)
- **Linux/macOS** (Windows possible via WSL)
- Acces reseau sortant vers `imap.gmail.com:993` (IMAP SSL uniquement)

### 1. Cloner et installer

```bash
git clone <url> email-learner
cd email-learner
./bootstrap.sh        # cree le venv, installe les deps, cree configs/.env
```

### 2. Configurer la connexion PostgreSQL

Editer `configs/.env` :

```bash
EMAIL_LEARNER_DB_PASSWORD=votre_mot_de_passe_postgres
```

Editer `configs/config.yaml`, remplacer `10.0.0.XXX` par l'IP de votre
serveur PostgreSQL (ou utiliser `localhost` si tout est sur la meme
machine).

### 3. Creer la base et lancer les migrations

```bash
psql -h <ip> -U postgres -c "CREATE DATABASE email_learner;"
psql -h <ip> -U postgres -c "CREATE USER email_learner_app WITH ENCRYPTED PASSWORD '...';"
psql -h <ip> -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE email_learner TO email_learner_app;"
make migrate
```

### 4. Configurer l'accès Gmail (IMAP)

Suivre le guide detaille dans [docs/SETUP-IMAP.md](docs/SETUP-IMAP.md)
ou simplement :

```bash
python -m src.main setup-imap    # guide + verification de connexion
```

En resume : activer la 2FA Google, creer un mot de passe d'application
(https://myaccount.google.com/apppasswords), activer l'IMAP dans Gmail,
puis ecrire `GMAIL_ADDRESS` et `GMAIL_APP_PASSWORD` dans `.env`.

### 5. Premier lancement

```bash
make run
```

Au premier demarrage, le daemon va :
1. Detecter qu'il n'y a pas encore de sync_state
2. Faire un **sync full** : recuperer les 6 derniers mois de mails
3. Inserer chaque mail dans la base (idempotent)
4. Generer les embeddings
5. Proposer des decisions (P1)
6. Afficher le dashboard sur le port 8080

---

## Utilisation quotidienne

### Commandes Make (raccourcis)

```bash
make help           # affiche toutes les commandes
make run            # lance le daemon en boucle
make dashboard      # lance juste le dashboard FastAPI
make health         # JSON etat de sante
make sync           # sync Gmail une fois (delta)
make sync-full      # sync Gmail complet (6 mois, pour rattrapage)
make test           # lance tous les tests
make logs           # tail des logs systemd
```

### Dashboard web

Accessible sur `http://10.0.0.XXX:8080` :

- **Vue d'ensemble** (`/`) : compteurs, phase actuelle, sante systeme
- **Mails** (`/mails.html`) : liste paginee, recherche
- **Decisions** (`/decisions.html`) : journal avec boutons
  Approuver / Rejeter pour les decisions P1
- **Stats** (`/stats.html`) : repartition actions, top expediteurs
- **Learning** (`/learning.html`) : precision par action, progression
  vers P2
- **Config** (`/config.html`) : toggles P2 et mode Vacances

### Activer P2 (autonomie)

1. Aller sur `/learning` et verifier que la precision est au-dessus
   des seuils (>95% pour archive, >90% pour mark_read, etc.)
2. Aller sur `/config`, cocher "Activer P2", Enregistrer
3. L'agent commence a executer les actions archive/mark_read/etc.
   automatiquement, sous les garde-fous
4. Surveiller `/api/health` et `/learning` regulierement
5. Pour arreter immediatement : decocher P2 dans `/config`

---

## Architecture (vue d'ensemble)

```
   Gmail API
       |
       v
  +---------+    +---------+    +----------+    +---------+
  |Observer |--->| Parser  |--->| Embedder |--->| DB      |
  +---------+    | (nh3)   |    | (bge-m3) |    |(Postgres|
       |         +---------+    +----------+    |+pgvector)|
       |                                          +---------+
       |                                               |
       |         +-------------+                       v
       |         | Rules       |<--+           +-----------+
       +-------->| Engine      |   |           | Decider   |
                 +-------------+   |           | (P2)      |
                       |          |           +-----------+
                       v          |                |
                 +---------+      |           +-----------+
                 |Recom-   |<-----+           | Action    |
                 |mender   |--+  RAG         | Worker    |
                 |(LLM)    |                  +-----------+
                 +---------+                       |
                       |                          v
                       v                     Gmail API
                 decision_journal
                 (append-only)
```

**Stack technique :** Python 3.11+, FastAPI, PostgreSQL + pgvector,
Ollama (bge-m3 + llama3.1:8b), Pydantic v2, Alembic.

---

## Tests

```bash
make test           # 238+ tests
make test-fast      # skip les lents
make test-contracts # uniquement les tests de contrat SQL
make test-coverage  # avec couverture
```

**Tests garantis :** anti-injection prompt, allowlist Gmail,
mots-cles critiques, idempotence, circuit-breaker, contrats SQL.

---

## Documentation complementaire

- [docs/SPEC-agent-mail-v5.md](docs/SPEC-agent-mail-v5.md) : specification
  complete
- [docs/PROMPTS-agent-mail.md](docs/PROMPTS-agent-mail.md) : prompts
  des 14 subagents
- [docs/PLAN-TRAVAIL-agent-mail.md](docs/PLAN-TRAVAIL-agent-mail.md) :
  plan d'execution
- [docs/OUTILS-agent-mail.md](docs/OUTILS-agent-mail.md) : dependances
  detaillees
- [docs/SETUP-IMAP.md](docs/SETUP-IMAP.md) : guide IMAP (app password)
- [docs/RUNBOOK.md](docs/RUNBOOK.md) : exploitation / backup
- [docs/REVIEW-ANGLES-MORTS-2026-07-17.md](docs/REVIEW-ANGLES-MORTS-2026-07-17.md) :
  audit externe et reponses
- [docs/PLAN-LIVRABLE-FINAL.md](docs/PLAN-LIVRABLE-FINAL.md) : plan
  pour finir a 100%

---

## Philosophie

> **L'autonomie se merite.**

Ce projet ne branche pas une IA sur votre boite mail en lui disant
"debrouille-toi". Il observe d'abord, puis propose, et seulement
quand la precision mesuree est suffisante, il agit. Et toujours sous
garde-fous, avec un kill-switch toujours disponible.

> **Vos donnees restent chez vous.**

Pas de LLM cloud. Pas d'envoi de mails sortants. Pas d'appels
tierces. Le daemon parle uniquement avec Gmail (IMAP SSL, mot de
passe d'application — envoi et suppression bloques par allowlist)
et Ollama (local).

> **Transparence totale.**

Chaque decision est expliquee en langage naturel, chaque action
est journalisee avec son contexte RAG, chaque garde-fou est testable.

---

## Licence

MIT (ou equivalente, voir le projet d'origine).
