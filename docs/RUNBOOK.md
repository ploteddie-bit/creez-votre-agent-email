# Runbook d'exploitation - Agent Mail 24/7

Ce document explique comment operer le systeme en production :
demarrage, arret, sauvegarde, restauration, monitoring, incidents.

---

## Demarrage

### Premier demarrage (apres installation)

```bash
# 1. Verifier la sante du systeme
make health

# 2. Si OK, lancer le daemon
make run
```

Le daemon va :
1. Charger la configuration depuis `configs/.env` et `configs/config.yaml`
2. Verifier la connexion PostgreSQL
3. Demarrer la boucle (sync -> embed -> recommend -> action)

### Demarrage en mode service (recommande en production)

Le projet fournit des fichiers systemd dans `systemd/`. Pour
installer :

```bash
# Copier les services
sudo cp systemd/email-learner.service /etc/systemd/system/
sudo cp systemd/email-learner-worker.service /etc/systemd/system/

# Activer et demarrer
sudo systemctl daemon-reload
sudo systemctl enable email-learner email-learner-worker
sudo systemctl start email-learner email-learner-worker

# Verifier le statut
sudo systemctl status email-learner
sudo systemctl status email-learner-worker

# Voir les logs
sudo journalctl -u email-learner -f
```

---

## Arret

```bash
# Arret graceful (SIGTERM)
sudo systemctl stop email-learner email-learner-worker

# Ou via la CLI (utilise SIGTERM)
make stop
```

Le daemon capture SIGTERM et finit le job en cours avant de s'arreter.
Attendre jusqu'a 30 secondes pour un arret complet.

Pour forcer (SIGKILL, dangereux, peut laisser des jobs en cours) :

```bash
sudo systemctl kill -s SIGKILL email-learner
```

---

## Monitoring

### Sante globale

```bash
make health
```

Retourne un JSON avec :
- `db_reachable` : PostgreSQL joignable
- `p2_enabled` : P2 ON ou OFF
- `kill_switch` : True = etat sur, False = P2 peut executer
- `observer.circuit_breaker` : quota Gmail utilise, retries
- `action_queue.by_status` : nombre de jobs par statut

### Dashboard web

Accessible sur `http://10.0.0.XXX:8080` :
- `/` : vue d'ensemble
- `/mails.html` : liste des mails
- `/decisions.html` : journal avec approve/reject
- `/stats.html` : graphiques
- `/learning.html` : metriques de precision
- `/config.html` : toggles P2 et vacation

### Logs

```bash
# Logs en temps reel
make logs

# Ou directement
sudo journalctl -u email-learner -f
```

Les logs sont structures :
```
2026-07-17T01:23:45+0200 [INFO] src.observer: sync done: 3 emails ingested
2026-07-17T01:23:46+0200 [INFO] src.embedder: embedded 5 emails
2026-07-17T01:23:47+0200 [INFO] src.recommender: processed 5 emails
2026-07-17T01:23:48+0200 [WARNING] src.observer: circuit-breaker TRIPPED
```

---

## Sauvegarde

### Variables d'environnement recommandees

Pour eviter de repeter `-h <host> -U email_learner_app` a chaque commande,
definir ces variables dans votre `~/.bashrc` ou dans le script :

```bash
export PGHOST=10.0.0.XXX        # hote PostgreSQL
export PGPORT=5432
export PGDATABASE=email_learner
export PGUSER=email_learner_app
# IMPORTANT: le mot de passe reste dans configs/.env, pas ici
export PGPASSWORD="$(grep EMAIL_LEARNER_DB_PASSWORD configs/.env | cut -d= -f2)"
```

### Base de donnees

```bash
# Dump complet
pg_dump -h <host> -U email_learner_app email_learner \
  > backup_$(date +%Y%m%d).sql

# Dump compresse (recommande)
pg_dump -h <host> -U email_learner_app email_learner | gzip \
  > backup_$(date +%Y%m%d).sql.gz
```

Duree typique : 2-10 min selon le volume. Les embeddings (vector(1024))
et les journaux (decision_journal, action_queue) representent
l'etat appris du systeme : les proteger imperativement.

### Automatisation (cron)

Ajouter dans `/etc/cron.d/agent-mailner-backup` :

```
# Backup quotidien a 3h du matin
0 3 * * * pg_learner /usr/local/bin/backup-email-learner.sh
```

Avec `backup-email-learner.sh` :

```bash
#!/bin/bash
set -e
BACKUP_DIR=/var/backups/email-learner
mkdir -p $BACKUP_DIR
pg_dump -h 10.0.0.XXX -U email_learner_app email_learner | \
  gzip > $BACKUP_DIR/backup_$(date +\%Y\%m\%d_\%H\%M).sql.gz

# Garder 30 jours locaux
find $BACKUP_DIR -name "backup_*.sql.gz" -mtime +30 -delete
```

### Retention

| Type | Local | NAS (recommande) |
|------|-------|------------------|
| Dump quotidien | 7 jours | 30 jours |
| Dump hebdomadaire | - | 90 jours |
| Dump mensuel | - | 1 an |

---

## Restauration

### A partir d'un dump

```bash
# 1. Arreter le daemon
sudo systemctl stop email-learner email-learner-worker

# 2. Supprimer la base existante
psql -h <host> -U postgres -c "DROP DATABASE email_learner;"
psql -h <host> -U postgres -c "CREATE DATABASE email_learner;"

# 3. Restaurer le dump
gunzip -c backup_20260716.sql.gz | psql -h <host> -U email_learner_app email_learner

# 4. Relancer le daemon
sudo systemctl start email-learner
```

### Test de restauration

Il est recommande de tester periodiquement la restauration (mensuel)
sur une base de test, pour verifier que les backups sont exploitables.

Procedure recommandee :

```bash
# 1. Arreter le daemon (pas obligatoire, mais prudent)
sudo systemctl stop email-learner email-learner-worker

# 2. Creer une base de test
psql -h <host> -U postgres -c "CREATE DATABASE email_learner_test;"

# 3. Restaurer le dernier dump
gunzip -c backup_$(date +%Y%m%d).sql.gz | \
  psql -h <host> -U email_learner_app email_learner_test

# 4. Verifier le contenu
psql -h <host> -U email_learner_app email_learner_test -c "SELECT COUNT(*) FROM emails;"
# Doit etre > 1000 (sinon le dump est vide)
psql -h <host> -U email_learner_app email_learner_test -c "SELECT COUNT(*) FROM decision_journal;"
psql -h <host> -U email_learner_app email_learner_test -c "SELECT COUNT(*) FROM email_embeddings;"

# 5. Nettoyer
psql -h <host> -U postgres -c "DROP DATABASE email_learner_test;"
```

Critere de succes : les 3 counts > 0. Si un count est 0, le backup
est corrompu ou incomplet.

---

## Incidents courants

### Le daemon ne demarre pas : "OAuth credentials not configured"

**Cause :** `configs/gmail-credentials.json` absent ou mal formate.

**Solution :**
```bash
# Verifier la presence
ls -la configs/gmail-credentials.json
# Si absent, suivre docs/SETUP-OAUTH.md

# Verifier le format
cat configs/gmail-credentials.json | python -m json.tool
```

### Le daemon crash avec "historyId expired"

**Cause :** Gmail expire les historyId apres ~7 jours. Si le daemon
est arrete plus de 7 jours, il doit faire un resync.

**Solution :** c'est automatique. Le daemon detecte le 404 et fait
un sync_full fallback (7 derniers jours, pas 6 mois - c'est le
quick win #4 de la Phase B).

### Circuit-breaker tripped

**Symptome :** dans `make health`, `circuit_breaker.paused = true`

**Cause :** quota Gmail > 80% ou > 100 messages/minute

**Solution :** attendre 10 minutes (pause automatique), puis le
circuit-breaker se reset. Si ca arrive souvent :
- Reduire la frequence de polling dans `configs/config.yaml`
- Verifier qu'on ne fait pas d'appels redondants

### Trop de decisions P1 en attente

**Symptome :** `/api/decisions?approved=false` retourne beaucoup
de resultats.

**Solution :**
- Aller sur `/decisions.html` et approuver/rejeter en masse
- Activer P2 si la precision est suffisante (page `/learning`)
- Activer le mode Vacances (page `/config`) pour stopper
  temporairement l'accumulation

### Erreurs "CircuitBreaker.paused"

Voir section ci-dessus.

---

## Mise a jour

```bash
# 1. Arreter le daemon
sudo systemctl stop email-learner

# 2. Sauvegarder la base (avant upgrade)
pg_dump email_learner > backup_before_upgrade.sql

# 3. Pull le nouveau code
git pull origin main

# 4. Mettre a jour les dependances
pip install -r requirements.txt

# 5. Lancer les migrations alembic
make migrate

# 6. Relancer
sudo systemctl start email-learner

# 7. Verifier
make health
make test
```

En cas de probleme, restaurer la base :

```bash
sudo systemctl stop email-learner
psql -U postgres -c "DROP DATABASE email_learner;"
psql -U postgres -c "CREATE DATABASE email_learner;"
psql -U email_learner_app email_learner < backup_before_upgrade.sql
sudo systemctl start email-learner
```

---

## Securite

### Rotation des secrets

**Mots de passe DB :** editer `configs/.env`, modifier
`EMAIL_LEARNER_DB_PASSWORD`, redemarrer le daemon.

**Credentials OAuth :** editer `configs/gmail-credentials.json`
(fichier complet) et supprimer `configs/token.json`. Le flow OAuth
se refait au prochain demarrage.

### Hardening systemd

Les fichiers `systemd/*.service` incluent deja :
- `NoNewPrivileges=true` : pas de privilege escalation
- `PrivateTmp=true` : /tmp isole
- `ProtectSystem=strict` : ecriture limitee aux chemins explicites
- `MemoryMax=512M` : limite memoire
- `CPUQuota=50%` : limite CPU

Pour aller plus loin, ajouter dans la section `[Service]` :

```ini
ProtectHome=true
ReadWritePaths=/home/eddie/email-learner/configs
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
```

### Logs sensibles

Les logs ne contiennent JAMAIS le contenu des mails (cf. SPEC §3.5).
Uniquement : sender + subject tronque.

---

## Contacts et escalade

- **Documentation :** `/docs/`
- **Logs :** `journalctl -u email-learner`
- **Sante :** `make health` ou `http://10.0.0.XXX:8080/api/health`
- **Spec :** `docs/SPEC-agent-mail-v5.md`
