# Configuration OAuth Gmail — pas à pas

Ce guide décrit comment créer les identifiants OAuth2 nécessaires pour que
l'agent puisse accéder à votre boîte Gmail **en lecture + modification
(scope `gmail.modify`)**, sans jamais pouvoir supprimer ni envoyer de mail.

> Temps réel : ~15 minutes. Vous devez avoir accès à
> [Google Cloud Console](https://console.cloud.google.com/).

---

## Préambule — sécurité

L'agent utilise **exclusivement** le scope :

```
https://www.googleapis.com/auth/gmail.modify
```

Ce scope autorise : lire les messages, gérer les labels, marquer comme lu/non lu,
archiver, étoiler, déplacer. Les méthodes **`delete`**, **`send`** et
**`drafts.send`** sont explicitement interdites dans l'allowlist de
`src/gmail_client.py` et **ne pourront jamais** être appelées par l'agent.

Aucune donnée ne quitte votre serveur : tout le traitement IA (embeddings,
LLM) se fait via Ollama en local.

---

## Étape 1 — Créer un projet Google Cloud

1. Rendez-vous sur <https://console.cloud.google.com/>.
2. En haut, cliquez sur le sélecteur de projet → **Nouveau projet**.
3. Nommez-le (ex. `agent-mail-24-7`), laissez l'organisation auto si vous n'en
   avez pas, puis **Créer**.
4. Une fois créé, sélectionnez-le dans le sélecteur de projet en haut.

## Étape 2 — Activer l'API Gmail

1. Dans le menu de gauche : **API et services → Bibliothèque**.
2. Recherchez **Gmail API**.
3. Ouvrez-la et cliquez sur **Activer**.

## Étape 3 — Configurer l'écran de consentement OAuth

Comme l'application n'est destinée qu'à vous-même (compte Gmail personnel),
on reste en mode **External** mais limité aux utilisateurs test.

1. **API et services → Écran de consentement OAuth**.
2. Choisissez **External** → **Créer**.
3. Renseignez :
   - **Nom de l'application** : `Agent Mail 24/7`
   - **E-mail d'assistance** : votre adresse
   - **Domaines autorisés** : (laisser vide si pas de domaine — OK pour test local)
   - **E-mails des utilisateurs de test** : **ajoutez votre propre adresse Gmail**
4. Enregistrez. À l'étape **Périmètres (scopes)**, ajoutez :
   - `https://www.googleapis.com/auth/gmail.modify` (sous `.../auth/gmail.modify`)
5. Finalisez l'écran (les autres sections — branding, etc. — restent optionnelles).

> ⚠️ Sans vous ajouter en **utilisateur test**, l'authentification échouera avec
> `access_denied` tant que l'app n'est pas validée par Google (long processus).

## Étape 4 — Créer les identifiants OAuth

1. **API et services → Identifiants → Créer des identifiants → ID client OAuth**.
2. Type d'application : **Application de bureau** (/Desktop app).
   - *Pas* « Application Web » : nous lançons un daemon local, pas un serveur web public.
3. Nommez-le (ex. `agent-mail-desktop`) → **Créer**.
4. Une fenêtre apparaît avec votre **ID client** et **Secret**.
   - Notez-les tout de suite, ou téléchargez le JSON.
5. Cliquez sur **Télécharger JSON**.

## Étape 5 — Placer les credentials dans le projet

Renommez le fichier téléchargé et placez-le :

```bash
# À la racine du projet
mv ~/Downloads/client_secret_*.json configs/gmail-credentials.json
```

Le fichier `.gitignore` exclut déjà `configs/gmail-credentials.json` : il ne
sera jamais commité.

Remplissez ensuite `configs/.env` avec les valeurs du JSON :

```dotenv
# configs/.env
EMAIL_LEARNER_GMAIL_CLIENT_ID=1234567890-xxxxxx.apps.googleusercontent.com
EMAIL_LEARNER_GMAIL_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxx
```

## Étape 6 — Premier run : consentement et refresh token

```bash
python -m src.main setup-oauth
```

Cette commande :

1. Ouvre (ou affiche) une URL Google d'autorisation.
2. Vous connectez avec votre compte Gmail (celui ajouté en utilisateur test).
3. Google affiche un avertissement « application non vérifiée » → cliquez sur
   **Avancé → Aller sur Agent Mail 24/7 (non sécurisé)**. C'est normal en mode
   test ; l'app est la vôtre.
4. Autorisez l'accès en lecture/modification.
5. Le refresh token est sauvegardé localement dans `configs/token.json`
   (également gitignoré).

À partir de là, l'agent peut rafraîchir son access token automatiquement,
sans intervention humaine.

## Vérification

```bash
# Doit afficher reachable: true pour gmail_observer
python -m src.main health
```

## Problèmes fréquents

| Symptôme | Cause | Solution |
|----------|-------|----------|
| `access_denied` au consentement | Votre compte n'est pas en utilisateur test | Étape 3.4 — ajoutez votre e-mail |
| `invalid_client` | ID/secret mal recopiés | Régénérez le JSON (Étape 4) |
| `redirect_uri_mismatch` | Type d'application = Web au lieu de Desktop | Recréez en type **Application de bureau** |
| `invalid_grant` au refresh | Token expiré ou révoqué | Relancez `setup-oauth` |
| `403 daily_quota` | Quota Gmail dépassé | Vérifiez `quota_used_today` dans `/api/stats` |

## Révocation

Pour couper l'accès à tout moment :
<https://myaccount.google.com/permissions> → retirez « Agent Mail 24/7 ».
Puis supprimez `configs/token.json` localement.
