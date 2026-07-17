# Guide IMAP Gmail — mot de passe d'application

Ce guide explique comment configurer l'accès IMAP du daemon à votre
compte Gmail via un **mot de passe d'application** Google.

> **Depuis le 2026-07-17, l'IMAP est le seul backend mail.** L'ancien
> backend OAuth / API Gmail a été supprimé (quotas, console Google
> Cloud, flow navigateur trop lourds). L'app password se configure en
> ~5 minutes et suffit pour tout ce que fait agent-mail : lire,
> lister, modifier des labels — **jamais envoyer, jamais supprimer**.

**Durée estimée : 5 minutes.**

---

## Vue d'ensemble

1. Activer la validation en 2 étapes sur le compte Google
2. Créer un mot de passe d'application (16 caractères)
3. Activer l'IMAP dans Gmail
4. Écrire `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` dans `.env`
5. Vérifier avec `python -m src.main setup-imap`

Sécurité : le daemon n'utilise que des commandes IMAP en lecture et
modification de labels. Les commandes d'envoi (`APPEND`) et de
suppression (`EXPUNGE`, `DELETE`) sont **bloquées par l'allowlist**
de `src/imap_client.py` — comme l'ancien scope `gmail.modify`, en
plus simple. Le mot de passe d'application ne quitte jamais la
machine et n'est jamais loggé.

---

## Étape 1 : Activer la validation en 2 étapes

Le mot de passe d'application exige la 2FA.

1. Aller sur https://myaccount.google.com/security
2. Section « Connexion à Google » > **Validation en 2 étapes**
3. Suivre l'activation (téléphone / clé)

---

## Étape 2 : Créer le mot de passe d'application

1. Aller sur https://myaccount.google.com/apppasswords
2. Nom de l'application : `agent-mail` (libre)
3. Cliquer **Créer**
4. Google affiche un mot de passe de 16 caractères
   (format `xxxx xxxx xxxx xxxx`) — **le copier tout de suite**, il
   n'est affiché qu'une fois.

> Les espaces sont optionnels : Google les accepte dans les deux
> formats. Conservez-les ou retirez-les, les deux fonctionnent.

---

## Étape 3 : Activer l'IMAP dans Gmail

1. Ouvrir Gmail > ⚙️ > **Voir tous les paramètres**
2. Onglet **Transfert et POP/IMAP**
3. Section « Accès IMAP » > **Activer l'IMAP**
4. Enregistrer les modifications

---

## Étape 4 : Configurer le projet

Écrire dans `.env` à la racine du projet (gitignoré) :

```dotenv
GMAIL_ADDRESS=votre.adresse@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

> `configs/.env` fonctionne aussi (lu en second). Ne jamais commiter
> ce fichier : il est couvert par `.gitignore`.

---

## Étape 5 : Vérifier la connexion

```bash
python -m src.main setup-imap
```

Sortie attendue :

```
OK — 15 labels visibles, 3 message(s) sur 7 jours.
Le daemon peut synchroniser la boîte.
```

Pour réafficher ce guide sans tester la connexion :

```bash
python -m src.main setup-imap --guide
```

---

## Dépannage

| Erreur | Cause | Solution |
|---|---|---|
| `IMAP login failed` | App password faux / révoqué | En recréer un (étape 2) |
| `SELECT ... failed (NO)` | Dossier « All Mail » non résolu | Le client le résout via le flag `\All` (RFC 6154) — vérifier que l'IMAP est activé |
| Timeout connexion | Réseau / pare-feu | Port 993 sortant requis vers `imap.gmail.com` |
| `GMAIL_ADDRESS / GMAIL_APP_PASSWORD manquants` | `.env` absent ou mal nommé | Vérifier l'emplacement (racine du projet) et les noms de variables |

## Révoquer l'accès

Pour couper l'accès du daemon : compte Google > Sécurité >
**Mots de passe des applications** > supprimer `agent-mail`.
La révocation est immédiate et ne touche pas les autres appareils.
