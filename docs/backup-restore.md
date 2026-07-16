# Sauvegarde et restauration PostgreSQL

Procédure de backup/restore pour la base `email_learner` (PostgreSQL 15 +
pgvector). Les embeddings (`vector(1024)`) et les journaux (`decision_journal`,
`action_queue`) représentent l'état appris du système : les protéger.

> Durée typique : sauvegarde 2–10 min selon le volume ; restauration identique.

---

## Variables

Adaptez ces variables à votre environnement (cf. `configs/config.yaml`) :

```bash
export PGHOST=10.0.0.XXX        # hôte PostgreSQL
export PGPORT=5432
export PGDATABASE=email_learner
export PGUSER=email_learner_app
export PGPASSWORD='<mot de passe de configs/.env>'
export BACKUP_DIR="$(pwd)/backups"
mkdir -p "$BACKUP_DIR"
```

> `PGPASSWORD` est lu par `pg_dump` / `pg_restore` ; ne le mettez pas
> en clair dans l'historique (`export PGPASSWORD='...'` puis `history -c`).

---

## 1. Sauvegarde complète

Format **custom** (compressé, supporte la restauration sélective) :

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
pg_dump -Fc \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" \
  -f "$BACKUP_DIR/email_learner-$STAMP.dump"
```

Vérifiez que le dump est lisible :

```bash
pg_restore -l "$BACKUP_DIR/email_learner-$STAMP.dump" | head
```

Vous devez voir la liste des tables. Si la sortie est vide, le dump est corrompu.

### Format SQL plein texte (alternative)

Pratique pour inspection / diff git :

```bash
pg_dump --no-owner --no-privileges \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" \
  -f "$BACKUP_DIR/email_learner-$STAMP.sql"
```

---

## 2. Restauration

### Cas A — Base inexistante (restauration initiale)

```bash
# 1. Créer la base + extensions
psql -h "$PGHOST" -U "$PGUSER" -d postgres <<'SQL'
CREATE DATABASE email_learner OWNER email_learner_app;
SQL

psql -h "$PGHOST" -U "$PGUSER" -d email_learner <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL

# 2. Restaurer
pg_restore -v --no-owner --no-privileges \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d email_learner \
  "$BACKUP_DIR/email_learner-YYYYMMDD-HHMMSS.dump"
```

### Cas B — Base existante (écrasement)

⚠️ **Destructif** : remplace toutes les données actuelles.

```bash
# 0. Sauvegarde de sécurité AVANT l'écrasement
STAMP=$(date +%Y%m%d-%H%M%S)
pg_dump -Fc -h "$PGHOST" -U "$PGUSER" "$PGDATABASE" \
  -f "$BACKUP_DIR/email_learner-PRE-RESTORE-$STAMP.dump"

# 1. Vider puis restaurer
pg_restore -v --clean --if-exists --no-owner --no-privileges \
  -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  "$BACKUP_DIR/email_learner-YYYYMMDD-HHMMSS.dump"
```

L'option `--clean --if-exists` supprime puis recrée chaque objet.

---

## 3. Sauvegarde des secrets runtime (hors base)

À backuper manuellement (sont dans `.gitignore`, jamais commités) :

| Fichier | Contenu | Critique ? |
|---------|---------|------------|
| `configs/.env` | Mot de passe DB, OAuth client/secret | Oui |
| `configs/config.yaml` | IPs, modèles, bind dashboard | Oui |
| `configs/gmail-credentials.json` | Client OAuth Google | Oui |
| `configs/token.json` | Refresh token Gmail | Oui (sinon re-setup-oauth) |

```bash
tar -czf "$BACKUP_DIR/configs-$STAMP.tgz" \
  configs/.env configs/config.yaml \
  configs/gmail-credentials.json configs/token.json
```

Chiffrez cette archive si elle quitte le serveur (`gpg -c ...`).

---

## 4. Automatisation (cron)

Backup nocturne, rotation sur 14 jours :

```cron
# crontab -e
30 3 * * *  PGHOST=10.0.0.XXX PGUSER=email_learner_app PGPASSWORD='...' \
  /usr/bin/pg_dump -Fc email_learner \
  -f /var/backups/email_learner-$(date +\%u).dump && \
  find /var/backups -name 'email_learner-*.dump' -mtime +14 -delete
```

Testez la restauration d'un backup **régulièrement** (au moins 1×/mois) :
un backup non restauré est un backup dont on ignore s'il fonctionne.

---

## 5. Vérification post-restauration

```bash
python -m src.main health
# -> postgresql.reachable doit être true
```

Puis ouvrez le dashboard `/stats` et vérifiez que les compteurs
(`total_emails`, `total_decisions`, `total_actions_done`) sont cohérents
avec l'état attendu.
