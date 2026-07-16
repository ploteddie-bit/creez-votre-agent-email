# Guide OAuth Gmail - etape par etape

Ce guide explique comment obtenir les credentials OAuth pour permettre
au daemon d'acceder a votre compte Gmail en lecture/ecriture limitee.

**Duree estimee : 10-15 minutes.**

---

## Vue d'ensemble

Le flux OAuth est le suivant :

1. Vous creez un projet Google Cloud
2. Vous activez l'API Gmail
3. Vous configurez l'ecran de consentement OAuth
4. Vous creez un identifiant OAuth 2.0 (type "Desktop app")
5. Vous telechargez le fichier JSON
6. Vous le placez dans `configs/gmail-credentials.json`
7. Au premier lancement du daemon, le flow OAuth se fait automatiquement

Le scope utilise est **`gmail.modify`** : permet de lire et modifier les
labels (archive, mark_read, star), mais **PAS d'envoyer ni de
supprimer** de mails.

---

## Etape 1 : Creer un projet Google Cloud

1. Aller sur https://console.cloud.google.com/
2. Cliquer sur le selecteur de projet (en haut a gauche) puis "New Project"
3. Nom : `agent-mail-24-7` (ou autre, c'est libre)
4. Organization : laisser par defaut
5. Cliquer "Create"

---

## Etape 2 : Activer l'API Gmail

1. Dans le menu hamburger (en haut a gauche) : `APIs & Services` > `Library`
2. Rechercher `Gmail API`
3. Cliquer sur "Gmail API"
4. Cliquer sur "Enable" (Activer)

---

## Etape 3 : Configurer l'ecran de consentement

1. Menu hamburger : `APIs & Services` > `OAuth consent screen`
2. Choisir `External` (pour usage personnel) ou `Internal` (si Workspace)
3. Cliquer "Create"
4. Remplir :
   - **App name** : `Agent Mail 24/7`
   - **User support email** : votre email
   - **Developer contact** : votre email
5. Cliquer "Save and Continue"
6. **Scopes** : cliquer "Add or Remove Scopes", chercher
   `https://www.googleapis.com/auth/gmail.modify`, le cocher, "Update"
7. Cliquer "Save and Continue"
8. **Test users** : cliquer "Add Users", ajouter votre email
9. Cliquer "Save and Continue"
10. Verifier le resume, cliquer "Back to Dashboard"

---

## Etape 4 : Creer un identifiant OAuth 2.0

1. Menu hamburger : `APIs & Services` > `Credentials`
2. Cliquer "Create Credentials" > "OAuth client ID"
3. **Application type** : choisir `Desktop app`
4. **Name** : `Agent Mail Daemon`
5. Cliquer "Create"
6. Une fenetre s'affiche avec client_id et client_secret
7. Cliquer "Download JSON" (le bouton en bas a droite)
8. Renommer le fichier telecharge en `gmail-credentials.json`

---

## Etape 5 : Placer le fichier dans le projet

```bash
# Le fichier telecharge doit etre dans configs/
cp ~/Downloads/gmail-credentials.json configs/gmail-credentials.json
```

Verifier que le fichier contient bien les cles :

```bash
cat configs/gmail-credentials.json | python -m json.tool
# Doit afficher : client_id, client_secret, project_id, etc.
```

---

## Etape 6 : Premier lancement du daemon

```bash
make run
```

Au premier lancement, le daemon va :

1. Detecter l'absence de `configs/token.json`
2. Ouvrir le flow OAuth dans le terminal :
   ```
   Please visit this URL to authorize this application: https://...
   ```
3. Vous ouvrez cette URL dans un navigateur
4. Vous vous connectez a votre compte Google
5. Vous autorisez l'application (ecran "This app isn't verified" :
   cliquer "Advanced" puis "Go to Agent Mail 24/7 (unsafe)" - c'est
   OK puisque c'est VOTRE app)
6. Vous obtenez un code que vous collez dans le terminal
7. Le daemon sauvegarde le token dans `configs/token.json`
8. Les sync suivantes utilisent ce token (refresh automatique)

---

## Etape 7 : Verifier

```bash
make health
```

Devrait retourner `"db_reachable": true` (ou false si pas de DB, mais
l'OAuth doit etre OK).

Verifier aussi que le token est bien sauvegarde :

```bash
ls -la configs/
# Doit montrer : gmail-credentials.json, token.json
```

---

## Renouvellement

Les tokens Gmail expirent apres ~1 heure, mais le daemon les
renouvelle automatiquement (refresh token). Si le refresh token est
revoque (par exemple, vous changez votre mot de passe Google), il
suffit de supprimer `configs/token.json` et de relancer le daemon
pour refaire le flow OAuth.

---

## Depannage

### "This app isn't verified"
- C'est normal pour une app en mode "Testing" (limite a 100 users)
- Cliquer "Advanced" > "Go to {app name} (unsafe)"
- C'est votre propre app, le risque est nul

### "Access blocked: This app's request is invalid"
- Verifier que l'API Gmail est bien activee dans le projet
- Verifier que le scope `gmail.modify` est bien dans la liste des scopes
- Verifier que votre email est dans "Test users"

### "redirect_uri_mismatch"
- Verifier que `http://localhost` est dans la liste des redirect URIs
- Aller dans Credentials > votre client OAuth > Authorized redirect URIs
- Ajouter `http://localhost` et `urn:ietf:wg:oauth:2.0:oob`

### Le daemon dit "credentials not configured"
- Verifier que `configs/gmail-credentials.json` existe
- Verifier qu'il contient `client_id` et `client_secret`

### "invalid_grant: Token has been expired or revoked"
- Supprimer `configs/token.json` et relancer

---

## Securite

- Le scope `gmail.modify` permet UNIQUEMENT de modifier les labels
  (archive, mark_read, star, IA-Review). Il ne peut PAS :
  - Envoyer des mails
  - Supprimer des mails
  - Creer des brouillons envoyables
  - Transférer des mails

- Le code defend en profondeur avec une allowlist stricte
  (voir `src/gmail_client.py`)

- Le token est stocke localement dans `configs/token.json`
  (gitignore, jamais committe)
