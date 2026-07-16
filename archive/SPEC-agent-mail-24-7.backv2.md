# SPEC — Agent Mail 24/7

> Spécification de conception — Agent autonome de gestion email  
> Créé : 2026-07-06  
> Statut : En revue (v2 — corrections intégrées)  
> Priorité : P0

---

## 1. Vue d'ensemble

### 1.1 Objectif

Agent autonome qui :
1. **Enregistre** tous les emails + toutes les actions utilisateur dans une base RAG (P0)
2. **Propose** des actions pour chaque mail entrant basé sur l'historique (P1)
3. **Décide** de façon autonome avec journal des décisions (P2)
4. **Expose** un dashboard HTML interactif 24/7 sur le réseau local

### 1.2 Principes

| Principe | Détail |
|----------|--------|
| **100% local** | Aucune donnée ne sort du réseau. IA locale (Ollama), base locale (PostgreSQL) |
| **Zéro auth local** | Pas de mot de passe, pas de token sur le réseau local |
| **Jamais de suppression** | L'IA ne supprime jamais un mail. Soft-delete uniquement (dossier IA-Review) |
| **Transparence totale** | Chaque décision IA est journalisée et visible dans le dashboard |
| **Apprentissage RAG (pas ML)** | L'IA apprend via Few-Shot dynamique (base vectorielle), **pas** via réentraînement. Chaque validation utilisateur enrichit le contexte pour les prochaines décisions. Le fine-tune est optionnel et ultérieur. |

### 1.3 Infrastructure cible

| Composant | Serveur | Adresse |
|-----------|---------|---------|
| Daemon + Dashboard | ia-general | 10.0.0.223 |
| PostgreSQL + pgvector + AGE | serveur-db | 10.0.0.166 |
| Ollama (IA locale) | ia-general | 10.0.0.223:11434 |
| Dashboard HTTP | ia-general | http://10.0.0.223:8080 |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ia-general (10.0.0.223)               │
│                                                          │
│  ┌──────────────┐    ┌──────────────┐   ┌────────────┐  │
│  │   Observer    │───▶│   Ingester   │──▶│  Embedder  │  │
│  │  (Gmail API   │    │  (parse +    │   │ (nomic-    │  │
│  │   polling)    │    │   detect     │   │  embed via │  │
│  │              │    │   actions)   │   │  Ollama)   │  │
│  └──────┬───────┘    └──────┬───────┘   └─────┬──────┘  │
│         │                   │                  │         │
│         │            ┌──────▼──────────────────▼──────┐  │
│         │            │     PostgreSQL (serveur-db)     │  │
│         │            │  ├── pgvector (embeddings)      │  │
│         │            │  ├── AGE (graphe relations)     │  │
│         │            │  └── tables (emails, actions)   │  │
│         │            └──────────────┬─────────────────┘  │
│         │                           │                    │
│  ┌──────▼───────┐         ┌────────▼────────┐           │
│  │  Recommender │         │  Dashboard HTTP  │           │
│  │  (P1: propose│         │  FastAPI + HTML  │           │
│  │   P2: decide)│         │  port 8080       │           │
│  └──────────────┘         └─────────────────┘           │
│                                                          │
│  ┌──────────────┐                                        │
│  │  Trainer     │  Timer systemd — 2h30 du matin         │
│  │  (QLoRA      │  Fine-tune nocturne (OPTIONNEL)        │
│  │   nocturne)  │  Seulement si RAG stagne (>500 items)  │
│  └──────────────┘                                        │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Phase P0 — Ingestion & RAG

### 3.1 Connexion Gmail

- **API** : Google Gmail API v1 via `google-api-python-client`
- **Auth** : OAuth2 avec token stocké en clair dans `~/.config/email-learner/token.json` (réseau local, pas de risque)
- **Scope** : `https://www.googleapis.com/auth/gmail.readonly`
- **Setup une seule fois** :
  1. Créer un projet Google Cloud Console
  2. Activer Gmail API
  3. Créer des credentials OAuth2 (type "Desktop app")
  4. Télécharger le `credentials.json`
  5. Lancer le flow OAuth une fois → le `token.json` est généré automatiquement
  6. Le refresh token assure l'accès permanent sans re-auth

### 3.2 Récupération initiale (historique)

Au premier lancement :
- Récupérer les **6 derniers mois** d'emails via `users().messages().list()`
- Pour chaque email : `users().messages().get(format='full')`
- Parser : headers (From, To, Date, Subject), body (text/html), labels, snippet
- Détecter l'état initial : lu/non lu (label `UNREAD`), présent (INBOX), archivé, supprimé (TRASH)
- **Traitement par batch** de 100 emails avec exponential backoff pour éviter le rate limiting Google (quota : 250 unités/sec)

### 3.3 Polling temps réel

- **Fréquence** : toutes les 2 minutes
- **Méthode** : `users().messages().list(q='newer_than:2m')` — récupère seulement les nouveaux
- **Optimisation** : stocker le `historyId` du dernier poll et utiliser `users().history().list(startHistoryId=...)` pour ne récupérer que les deltas (plus efficace)

### 3.4 Détection des actions utilisateur

À chaque poll, comparer l'état actuel des emails avec l'état précédent :

| Delta de labels détecté | Action enregistrée |
|--------------------------|-------------------|
| `INBOX` → absent | Supprimé (soft) |
| `INBOX` → `TRASH` | Supprimé |
| `INBOX` → absent (pas dans TRASH) | Archivé |
| `UNREAD` présent → absent | Lu |
| `STARRED` absent → présent | Étoilé |
| Nouveau message dans `INBOX` | Nouveau mail entrant |
| Réponse détectée (thread avec body de l'utilisateur) | Répondu |

### 3.5 Base de données

**PostgreSQL sur serveur-db** avec extensions :

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector
CREATE EXTENSION IF NOT EXISTS age;          -- Apache AGE
LOAD 'age';

-- Table principale
CREATE TABLE emails (
    id              TEXT PRIMARY KEY,        -- Gmail message ID
    thread_id       TEXT,
    sender          TEXT NOT NULL,
    sender_email    TEXT NOT NULL,
    recipients      TEXT[],
    subject         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    date_received   TIMESTAMPTZ NOT NULL,
    labels          TEXT[],                  -- Labels Gmail bruts
    is_read         BOOLEAN,
    is_starred      BOOLEAN,
    is_deleted      BOOLEAN DEFAULT FALSE,
    is_archived     BOOLEAN DEFAULT FALSE,
    raw_headers     JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Actions utilisateur
CREATE TABLE email_actions (
    id              SERIAL PRIMARY KEY,
    email_id        TEXT REFERENCES emails(id),
    action          TEXT NOT NULL,           -- 'read', 'deleted', 'archived', 'starred', 'replied', 'ignored'
    detected_at     TIMESTAMPTZ DEFAULT NOW(),
    detected_by     TEXT DEFAULT 'poll_delta'
);

-- Embeddings pour recherche de similarité
CREATE TABLE email_embeddings (
    email_id        TEXT PRIMARY KEY REFERENCES emails(id),
    embedding       vector(1024),            -- Dimension de nomic-embed-text / bge-m3
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Index pour recherche rapide
CREATE INDEX ON email_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON emails (sender_email);
CREATE INDEX ON emails (date_received DESC);
CREATE INDEX ON email_actions (email_id);
CREATE INDEX ON email_actions (action);

-- Journal des décisions IA
CREATE TABLE decision_journal (
    id              SERIAL PRIMARY KEY,
    email_id        TEXT REFERENCES emails(id),
    phase           TEXT NOT NULL,           -- 'P1_proposal', 'P2_auto'
    proposed_action TEXT NOT NULL,
    confidence      FLOAT,                  -- 0.0 à 1.0
    similar_emails  TEXT[],                  -- IDs des 5 mails de référence
    user_approved   BOOLEAN,                -- NULL pour P2 auto, TRUE/FALSE pour P1
    actual_action   TEXT,
    justification   TEXT,                    -- Explication de la recommandation
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Métriques d'apprentissage
CREATE TABLE learning_metrics (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    total_emails    INT,
    total_actions   INT,
    p1_proposals    INT,
    p1_approved     INT,
    p1_rejected     INT,
    p2_auto_actions INT,
    p2_correct      INT,                    -- Vérifiable si l'utilisateur corrige après
    accuracy_p1     FLOAT,
    accuracy_p2     FLOAT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Graphe AGE : relations entre expéditeurs
SELECT create_graph('email_graph');

-- Nœuds : expéditeurs
-- Arêtes : communication entre expéditeurs (qui écrit à qui, fréquence, sujets)
```

### 3.6 Connexion PostgreSQL

- **Méthode** : tunnel SSH depuis ia-general vers serveur-db
- **Commande** : `ssh -L 15432:localhost:5432 eddie@serveur-db`
- **Service systemd** : `email-learner-pg-tunnel.service` pour maintenir le tunnel
- **Port local** : `15432` → PostgreSQL sur serveur-db
- **Auth PG** : trust (réseau local, pas de mot de passe)

### 3.7 Embeddings

- **Modèle** : `nomic-embed-text` via Ollama (à installer si absent)
- **Alternative** : `bge-m3` (déjà utilisé dans le RAG ExploDev)
- **Chaque email** reçoit un vecteur de 1024 dimensions
- **Recherche** : `SELECT * FROM email_embeddings ORDER BY embedding <-> $query_vec LIMIT 5` pour les 5 mails les plus similaires

---

## 4. Mécanisme d'apprentissage (CRITIQUE — clarification)

> **Point clé** : cet agent ne "s'entraîne" pas au sens Machine Learning en P0/P1. Il utilise le RAG comme mémoire contextuelle. Le fine-tune (réentraînement réel des poids du modèle) est optionnel et vient APRÈS.

### 4.1 Comment l'IA "apprend" sans réentraînement

Le mécanisme est du **Few-Shot dynamique via base vectorielle** :

```
Nouveau mail entrant
       │
       ▼
┌─────────────────┐
│ Embedding du     │
│ mail (Ollama)    │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ Recherche vectorielle pgvector      │
│ → 5 mails les plus similaires       │
│ → Récupérer les ACTIONS validées    │
│   que l'utilisateur a prises dessus │
└────────┬────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ Construction du prompt Few-Shot :   │
│                                     │
│ "Voici 5 mails similaires passés    │
│  et les actions que l'utilisateur   │
│  a prises dessus :                  │
│  1. [mail similaire] → archivé      │
│  2. [mail similaire] → répondu      │
│  3. [mail similaire] → archivé      │
│  ...                                │
│                                     │
│  Nouveau mail : [mail entrant]      │
│  Quelle action recommandes-tu ?"    │
└────────┬────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ L'IA répond avec une action +       │
│ confiance + justification           │
└─────────────────────────────────────┘
```

**Pourquoi ça "apprend"** :
- En P0 : la base est vide → pas de recommandation possible. L'IA observe et collecte.
- En P1 (début) : peu de données → recommandations faibles. L'utilisateur valide ou corrige. **Chaque correction va dans `decision_journal` avec `user_approved = true/false`.**
- En P1 (après ~100 mails) : la base contient assez de patterns → les 5 mails similaires sont pertinents → les recommandations s'améliorent naturellement.
- En P2 : même mécanisme, mais l'IA exécute seule. Les corrections utilisateur (quand l'IA se trompe) enrichissent aussi la base.

**L'apprentissage est la boucle** : action utilisateur → stockée dans la base → reconsultée lors du prochain mail similaire. C'est de la **mémoire contextuelle**, pas du gradient descent.

### 4.2 Différence RAG vs Fine-tune

| | RAG (P0/P1/P2) | Fine-tune (optionnel, après P1) |
|---|---|---|
| **Quoi** | Recherche de contexte dans la base | Réentraînement des poids du modèle |
| **Quand** | Temps réel, à chaque mail | Nocturne (2h30) |
| **Latence** | < 200ms (requête PG) | Heures (training) |
| **Données** | Tous les emails + actions | Seulement les paires validées |
| **Effet** | L'IA voit des exemples pertinents | L'IA intègre les patterns dans ses poids |
| **Nécessaire ?** | **OUI — cœur du système** | NON — amélioration optionnelle |

### 4.3 Quand le fine-tune devient pertinent

Le fine-tune (section 8) n'est activé que si :
- Plus de **500 interactions validées** dans `decision_journal`
- L'**accuracy P1 stagne** (ex: < 80% malgré 200+ mails)
- On veut **réduire la latence** (le modèle intègre les patterns sans chercher dans le RAG)

En attendant, le RAG seul suffit. Le fine-tune est un upgrade, pas une nécessité.

---

## 5. Phase P1 — Copilote (apprentissage actif)

### 5.1 Fonctionnement

Quand un nouveau mail arrive :

1. **Embedding** du mail entrant
2. **Recherche vectorielle** : 5 mails les plus similaires dans la base
3. **Récupération des actions** : quelles actions l'utilisateur a-t-il prises sur ces 5 mails ?
4. **Construction du prompt Few-Shot** : les 5 exemples + le nouveau mail
5. **L'IA propose** une action avec un score de confiance
6. **Le dashboard affiche** la proposition (temps réel via WebSocket)
7. **L'utilisateur valide ou rejette** (boutons dans le dashboard)
8. **Stockage** : la décision (avec le label user_approved) va dans `decision_journal`

### 5.2 Format de proposition

```json
{
  "email_id": "18a3f...",
  "sender": "boss@corp.com",
  "subject": "Réunion demain",
  "proposed_action": "répondre",
  "confidence": 0.87,
  "justification": "3 des 5 mails similaires de cet expéditeur ont reçu une réponse en < 1h. Le sujet contient 'réunion' = priorité haute.",
  "similar_emails": [
    {"id": "18a1...", "subject": "Réunion vendredi", "action": "répondu"},
    {"id": "17f2...", "subject": "Point projet", "action": "répondu"},
    {"id": "16b4...", "subject": "Newsletter tech", "action": "archivé"},
    {"id": "15c8...", "subject": "Réunion planning", "action": "répondu"},
    {"id": "14d1...", "subject": "Info générale", "action": "lu"}
  ]
}
```

### 5.3 Calcul de confiance

```
confiance = (nb_similaires_même_action / 5) * facteur_expéditeur * facteur_ancienneté

facteur_expéditeur = 1.0 + (0.1 * nb_interactions_précédentes_cet_expéditeur)  (cap à 1.5)
facteur_ancienneté = 0.8 si mail_similaire > 90 jours, 1.0 sinon
```

### 5.4 Transition vers P2

Critères pour activer P2 :
- Minimum **200 emails** dans la base
- Minimum **50 propositions P1** traitées
- **Accuracy P1 > 80%** (propositions approuvées / total propositions)
- L'utilisateur active **manuellement** P2 dans le dashboard (jamais automatique)

---

## 6. Phase P2 — Autonome (avec garde-fous anti-hallucination)

### 6.1 Fonctionnement

L'IA agit **sans demander confirmation**. Chaque action est :
1. Exécutée via Gmail API (modifier labels)
2. Journalisée dans `decision_journal` avec `phase = 'P2_auto'`
3. Visible immédiatement dans le dashboard

### 6.2 Garde-fous non négociables

| Garde-fou | Implémentation | Pourquoi |
|-----------|---------------|----------|
| **Jamais de suppression** | L'IA ne supprime JAMAIS. Déplacement vers label `IA-Review` uniquement. L'utilisateur peut récupérer. | Anti-hallucination : si l'IA se trompe, le mail n'est jamais perdu |
| **Seuil de confiance** | Actions autonomes seulement si `confidence >= 0.85` | En dessous, forcer P1 (proposition) |
| **Kill-switch** | Toggle dans le dashboard : OFF = repasse en P1 immédiatement | Contrôle humain permanent |
| **Quota quotidien** | Max 20 actions/jour (configurable) | Limite les dégâts en cas de bug |
| **Actions autorisées limitées** | Seulement : marquer lu, archiver, étoiler. PAS : répondre, transférer, supprimer | Réduit la surface d'erreur |
| **Revue obligatoire** | Si le mail vient d'un expéditeur JAMAIS vu → forcer P1 | L'IA ne peut pas deviner sur un pattern inconnu |
| **Journal complet** | Chaque décision P2 est loggée avec justification et mails de référence | Auditabilité totale |
| **Correction utilisateur trackée** | Si l'utilisateur inverse une action P2 → enregistré comme `p2_correct = false` | Mesure la vraie fiabilité de P2 |

### 6.3 Actions autorisées en P2

| Action | Autorisée | Méthode Gmail API |
|--------|-----------|-------------------|
| Marquer comme lu | Oui | `modify(removeLabelIds: ['UNREAD'])` |
| Archiver | Oui | `modify(removeLabelIds: ['INBOX'])` |
| Étoiler | Oui | `modify(addLabelIds: ['STARRED'])` |
| Déplacer vers IA-Review | Oui | `modify(addLabelIds: ['Label_IA_Review'])` |
| Répondre | **NON** | — |
| Supprimer | **NON** | — |
| Transférer | **NON** | — |

---

## 7. Dashboard HTTP 24/7

### 7.1 Stack technique

| Couche | Technologie |
|--------|-------------|
| Backend | FastAPI (Python) |
| Frontend | HTML + CSS + vanilla JS |
| Graphiques | Chart.js |
| Mise à jour temps réel | WebSocket (FastAPI WebSocket) |
| Port | 8080 |
| Hôte | 0.0.0.0 (accessible sur tout le réseau local) |
| Auth | **Aucune** (réseau local, zéro mot de passe) |

### 7.2 Pages du dashboard

#### Page 1 — Vue d'ensemble (`/`)
- Compteurs : total mails, mails aujourd'hui, actions par type
- Phase actuelle (P0 / P1 / P2) avec indicateur visuel
- Derniers événements (5 derniers mails + actions)
- Bouton kill-switch P2

#### Page 2 — Flux mail (`/mails`)
- Tableau paginé de tous les mails
- Colonnes : Date, Expéditeur, Sujet, Action, Lu, Phase
- Filtres : par expéditeur, par action, par date, par phase
- Cliquer sur un mail → détail complet + mails similaires + décision IA

#### Page 3 — Décisions (`/decisions`)
- Journal des décisions IA (P1 et P2)
- Pour P1 : boutons Approuver / Rejeter
- Pour P2 : badge "Autonome" + lien vers le mail de référence
- Filtrable par phase, confiance, résultat

#### Page 4 — Statistiques (`/stats`)
- **Graphique 1** : Répartition des actions (camembert) — lu, archivé, supprimé, étoilé, répondu, ignoré
- **Graphique 2** : Actions par jour (barres) sur les 30 derniers jours
- **Graphique 3** : Top 10 expéditeurs (barres horizontales)
- **Graphique 4** : Heures d'activité (heatmap)

#### Page 5 — Apprentissage (`/learning`)
- **Courbe** : Accuracy P1 au fil du temps (propositions approuvées / total)
- **Courbe** : Accuracy P2 (actions correctes / total actions autonomes)
- **Indicateur** : Progression vers P2 (200 mails / 50 propositions / 80% accuracy)
- **Tableau** : Expéditeurs les mieux appris (haute confiance) vs les moins bien appris
- **Graphe** : Visualisation AGE des relations entre expéditeurs

#### Page 6 — Configuration (`/config`)
- Kill-switch P2 (on/off)
- Seuil de confiance (slider 0.5 → 0.99)
- Quota quotidien P2 (nombre)
- Modèle IA utilisé (dropdown des modèles Ollama)
- Fréquence de polling (minutes)
- Bouton : Forcer sync Gmail maintenant
- Bouton : Exporter la base complète en JSON

### 7.3 API REST

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/api/emails` | GET | Liste paginée des emails |
| `/api/emails/{id}` | GET | Détail d'un email + mails similaires |
| `/api/actions` | GET | Liste des actions |
| `/api/decisions` | GET | Journal des décisions |
| `/api/decisions/{id}/approve` | POST | Approuver une proposition P1 |
| `/api/decisions/{id}/reject` | POST | Rejeter une proposition P1 |
| `/api/stats` | GET | Statistiques globales |
| `/api/learning` | GET | Métriques d'apprentissage |
| `/api/config` | GET/PUT | Configuration |
| `/api/sync` | POST | Forcer sync Gmail |
| `/api/ws` | WebSocket | Événements temps réel (nouveau mail, nouvelle décision) |

---

## 8. Fine-tune (optionnel — après P1)

### 8.1 Quand fine-tuner

Le fine-tune est **optionnel** et vient APRÈS la phase P1. Le RAG seul (Few-Shot dynamique) suffit pour P1 et P2. Le fine-tune améliore la qualité quand :
- Plus de **500 interactions validées** existent dans `decision_decision`
- L'**accuracy P1 stagne** (< 80% malgré assez de données)
- On veut que le modèle intègre les patterns **dans ses poids** sans chercher dans le RAG à chaque fois

### 8.2 Méthode

- **Modèle** : Qwen2.5-7B-Instruct (supporté par Ollama, tient en QLoRA sur RX 9060 XT)
- **Méthode** : QLoRA via le venv `unsloth-env` sur ia-general
- **Données** : paires (mail → action validée) depuis `decision_journal` où `user_approved = TRUE`
- **Fréquence** : nocturne (2h30), timer systemd
- **Déploiement** : GGUF → `ollama create` (remplace le modèle précédent après validation)

### 8.3 Format SFT

```json
{"messages": [
  {"role": "system", "content": "Tu es l'assistant mail d'Eddie. Analyse ce mail et recommande une action parmi: lire, archiver, étoiler, ignorer, répondre. Réponds en JSON."},
  {"role": "user", "content": "De: boss@corp.com | Sujet: Réunion demain 9h | Corps: Bonjour, peux-tu confirmer ta présence ?"},
  {"role": "assistant", "content": "{\"action\": \"répondre\", \"confiance\": 0.92, \"raison\": \"Question directe de mon supérieur, nécessite confirmation rapide\"}"}
]}
```

---

## 9. Fichiers et structure du projet

```
/home/eddie/email-learner/
├── src/
│   ├── main.py              # Point d'entrée daemon
│   ├── observer.py           # Polling Gmail API
│   ├── parser.py             # Parsing emails
│   ├── ingester.py           # Insertion PostgreSQL
│   ├── action_detector.py    # Détection des actions par delta
│   ├── embedder.py           # Génération embeddings via Ollama
│   ├── recommender.py        # P1: propositions Few-Shot dynamique
│   ├── decider.py            # P2: décisions autonomes + garde-fous
│   ├── dashboard.py          # FastAPI + HTML + WebSocket
│   ├── trainer.py            # Fine-tune QLoRA nocturne (optionnel)
│   └── config.py             # Configuration
├── static/
│   ├── index.html            # Vue d'ensemble
│   ├── mails.html            # Flux mail
│   ├── decisions.html        # Décisions
│   ├── stats.html            # Statistiques
│   ├── learning.html         # Apprentissage
│   ├── config.html           # Configuration
│   ├── style.css
│   └── app.js
├── systemd/
│   ├── email-learner.service         # Daemon principal
│   ├── email-learner-pg-tunnel.service  # Tunnel SSH PostgreSQL
│   ├── email-learner-train.timer     # Timer fine-tune (optionnel)
│   └── email-learner-train.service   # Service fine-tune (optionnel)
├── configs/
│   ├── config.yaml           # Configuration générale
│   └── gmail-credentials.json # OAuth2 (gitignored)
├── data/
│   └── embeddings_cache/     # Cache local des embeddings
├── tests/
│   ├── test_observer.py
│   ├── test_ingester.py
│   └── test_recommender.py
├── requirements.txt
└── README.md
```

---

## 10. Dépendances Python

```
google-api-python-client    # Gmail API
google-auth-oauthlib        # OAuth2
google-auth-httplib2        # OAuth2 transport
psycopg2-binary             # PostgreSQL
pgvector                    # Recherche vectorielle
asyncio                     # Async daemon
fastapi                     # Dashboard backend
uvicorn                     # ASGI server
jinja2                      # Templates HTML
websockets                  # Temps réel
httpx                       # Requêtes HTTP
pyyaml                      # Configuration
schedule                    # Scheduling interne (backup du timer systemd)
```

---

## 11. Ordre d'implémentation

| Étape | Phase | Description | Critère de validation |
|-------|-------|-------------|----------------------|
| 1 | P0 | Setup PostgreSQL + pgvector + AGE sur serveur-db | Tables créées, extensions actives |
| 2 | P0 | Tunnel SSH systemd ia-general → serveur-db | `psql -p 15432` connecte |
| 3 | P0 | Install `nomic-embed-text` sur Ollama | `ollama pull nomic-embed-text` OK |
| 4 | P0 | OAuth2 Gmail setup + test | `observer.py` récupère 1 email |
| 5 | P0 | Ingester complet | 100 emails historiques dans PostgreSQL |
| 6 | P0 | Embedder | Embeddings générés pour les 100 emails |
| 7 | P0 | Action detector | Actions détectées par delta de polling |
| 8 | P0 | Dashboard (page Vue d'ensemble) | http://ia-general:8080 affiche les compteurs |
| 9 | P0 | Sync historique complète (6 mois) | Tous les emails dans la base |
| 10 | P1 | Recommender (Few-Shot dynamique) | Proposition générée pour un mail entrant |
| 11 | P1 | Dashboard (page Décisions) | Approuver/Rejeter fonctionne |
| 12 | P1 | Dashboard complet (toutes les pages) | Toutes les pages fonctionnent |
| 13 | P1 | Métriques d'apprentissage | Courbe accuracy visible |
| 14 | P2 | Decider (autonome + garde-fous) | Actions P2 exécutées, soft-delete, journal |
| 15 | P2 | Kill-switch + quotas + correction tracking | Tout fonctionne |
| 16 | (opt) | Trainer QLoRA nocturne | Fine-tune déployé via `ollama create` |

---

## 12. Risques identifiés

| Risque | Impact | Mitigation |
|--------|--------|------------|
| Rate limiting Gmail API | P0 bloqué | Batch 100/batch, exponential backoff, historyId pour deltas |
| Taille de la base (milliers d'emails × embeddings) | Performance | Index IVFFlat pgvector, partitionnement par date |
| Hallucination IA en P2 | Action incorrecte | Soft-delete (IA-Review), seuil 85%, quota, kill-switch, correction tracking |
| OAuth2 token expiration | P0 cassé | Refresh token automatique, alerte dashboard |
| Modèle local trop lent pour le temps réel | Latence | nomic-embed-text rapide (< 100ms), recommender = RAG pas le modèle LLM |
| Tunnel SSH instable | Base inaccessible | Auto-reconnect systemd, retry avec backoff |
| Peu de données au démarrage (cold start) | Recommandations faibles | Phase P0 collecte d'abord, P1 commence seulement après accumulation |

---

## 13. Questions ouvertes

| Question | Statut |
|----------|--------|
| Quel modèle Ollama pour le prompt Few-Shot en P1 ? (gpt-oss-20b ou Qwen2.5-14B) | À trancher |
| Notification desktop quand une proposition P1 arrive ? | Optionnel |
| Multi-comptes Gmail ? | Futur |
