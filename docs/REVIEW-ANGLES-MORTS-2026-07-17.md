# Avis sur l'audit externe — Angles morts & Améliorations

**Date :** 2026-07-17
**Source de l'audit :** avis extérieur reçu le 16/07/2026 vers 00h30
**Auteur de cette réponse :** Mavis (Mavis/MiMo)
**Contexte :** revue du projet Agent Mail 24/7 (post-implémentation P0/P1/P2)

---

## Verdict global

L'avis est globalement **pertinent et bien construit**. Environ 70 % des points
identifiés sont de vrais angles morts qui causeraient des problèmes en
production. Les 30 % restants sont soit déjà couverts par l'implémentation
actuelle, soit du produit v2+ qui sort du périmètre immédiat.

**Taux de pertinence : 7/10.**

---

## Partie 1 — Les angles morts où je suis d'accord (à régler)

### 1.1 Explicabilité + correction rapide (PRIORITÉ #1)

**Le problème :** aujourd'hui, le `decision_journal` stocke une décision avec
`reason` (string court) et `confidence` (float). L'utilisateur voit
"archive, confidence 0.85" mais ne sait pas pourquoi. Aucun bouton
pour transformer une correction en apprentissage visible.

**Pourquoi c'est un vrai angle mort :** c'est ce qui crée l'adoption ou
l'abandon. L'avis a raison, un humain qui ne comprend pas une décision
la rejette ou désinstalle. Un humain qui voit "Archivé car similaire à
12 newsletters de ce domaine, dernière action confirmée il y a 3 jours"
accepte et fait confiance.

**Action concrète :**
- ajouter `decision_rationale` (string lisible) à `MailDecision` et
  `decision_journal`, généré par template déterministe (pas besoin d'un
  LLM pour expliquer)
- exposer dans le dashboard via le panneau de décision
- ajouter endpoint `POST /api/decisions/{id}/apply_to_similar` qui
  propage la correction aux 15 derniers mails similaires

**Effort : ~25 min, 0 régression.**

### 1.2 "L'IA archive trop vite les factures" + PJ non classées

**Le problème :** un PDF sans texte extractible (PDF scanné) passe entre
les mailles du Rules Engine. Le sujet "Votre document" n'a pas de
mot-clé critique, donc l'IA peut classer ça en `archive` après quelques
semaines de RAG.

**Pourquoi c'est un vrai angle mort :** c'est de la sécurité, pas de
l'UX. Un faux archivage de facture peut avoir des conséquences réelles
(paiement en retard, amende).

**Action concrète :**
- ajouter une règle dans `rules_engine.py` :
  - si `has_attachments == True` ET
  - email n'a jamais été manuellement classifié par l'utilisateur
  - alors `move_ia_review` avec confidence haute
- cette règle est **absolue** : aucune PJ non encore vue ne peut être
  auto-classifiée

**Effort : ~15 min.**

### 1.3 Encodage MIME robuste (chardet)

**Le problème :** le `parser.py` actuel suppose UTF-8 partout. Un mail
français de 2008 peut être en ISO-8859-1, Windows-1252, ou quoted-
printable cassé. Le `decode_base64url` retourne du texte corrompu, le
nettoyage nh3 ne répare pas, et l'embedding suivant est faux.

**Pourquoi c'est un vrai angle mort :** sans données propres, tout le
reste (RAG, classification) est sur du sable. L'avis a raison de le
classer en priorité.

**Action concrète :**
- ajouter `chardet>=5.0.0` à `requirements.txt`
- dans `parser.py`, détecter l'encodage du payload avant
  `decode_base64url` (utiliser `chardet.detect()` sur les bytes bruts)
- fallback UTF-8 si chardet n'est pas sûr

**Effort : ~20 min.**

### 1.4 Idempotence Gmail fragile (historyId expire ~7 jours)

**Le problème :** Gmail expire les `historyId` après environ 7 jours
d'inactivité. Si le serveur local tombe en panne pendant une semaine
(vacances, crash disque), le `sync_delta()` va recevoir un 404 et
basculer sur `sync_full()`. Le `sync_full()` actuel fait
`messages.list(q='newer_than:6m')` qui peut re-rapporter 6000 mails
d'un coup, ce qui sature le quota et fait doublonner.

**Pourquoi c'est un vrai angle mort :** c'est un scénario réel (panne
pendant les vacances). Le premier crash long = reset de 6 mois de
données + quota saturé.

**Action concrète :**
- modifier `sync_full()` pour utiliser `newer_than:7d` quand il est
  invoqué comme fallback (vs. 6 mois au premier lancement initial)
- ajouter une colonne `last_attempted_history_id` séparée de
  `last_history_id` pour pouvoir détecter les coupures longues

**Effort : ~30 min.**

### 1.5 Cold start RAG brutal (Shadow Mode)

**Le problème :** avec peu d'historique et peu de feedback initial, le
RAG va halluciner des actions génériques pendant les premières
semaines. Le Recommender va proposer des `archive` ou `none` au
hasard, ce qui pollue le `decision_journal` et fausse les stats.

**Pourquoi c'est un vrai angle mort :** l'avis a raison, c'est un
pattern MLOps classique. L'idéal serait une phase de calibration
silencieuse.

**Action concrète (à faire APRÈS P1, pas ce soir) :**
- ajouter un mode `shadow` au Recommender : pendant 2 semaines, il
  calcule la décision mais ne la persiste pas (ou la persiste avec un
  flag `shadow=True`)
- les seuils P2 se calibrent sur les décisions shadow avant activation

**Effort : ~1h. Pas urgent.**

---

## Partie 2 — Les points que je challenge

### 2.1 "Fast Path" pour newsletters → bypass sandbox/LLM

**L'avis dit :** ajouter un mode Fast Path qui bypass complètement la
sandbox et le LLM pour les emails triviaux (newsletters identifiées
par règle statique).

**Mon contre-argument :** c'est **déjà fait** par le Rules Engine.
La règle 1 (`noreply + low_priority_domain`) court-circuite le
LLM avant tout appel Ollama. Le pipeline complet ne se déclenche pas
dans ce cas. C'est exactement ce que l'avis demande, juste formulé
différemment.

**Preuve dans le code :** `src/recommender.py`, fonction `recommend()`,
étape 1 : "Rules engine -> si match critique, retour immédiat". Le
LLM n'est appelé qu'en étape 4, après que les règles statiques
aient filtré les cas triviaux.

**Action : aucune, déjà couvert.**

### 2.2 Multi-account unifié

**L'avis dit :** supporter plusieurs comptes Gmail (pro + perso)
avec unification et séparation stricte des contextes RAG.

**Mon contre-argument :** la SPEC v5 le tag explicitement comme P3.
C'est un gros chantier (OAuth séparé par compte, RAG par compte,
risk de fuite entre contextes). À faire après la v1 stable, pas
maintenant.

**Action : aucune, hors scope v0.**

### 2.3 Privacy Audit Log dédié

**L'avis dit :** un tableau de bord dédié montrant quelles données
ont été traitées, quand, et confirmant qu'aucune donnée n'a quitté
le LAN.

**Mon contre-argument :** c'est un produit en soi, pas une feature.
L'information existe déjà (les `decision_journal`, `action_queue`,
`email_embeddings` ont tous des timestamps). Il suffit d'une vue SQL
agrégée pour le dashboard. Pas besoin d'un système dédié.

**Action : ajouter une vue SQL agrégée dans une itération future.**

### 2.4 Intégration CalDAV / Todo list locale

**L'avis dit :** si l'email contient une date ("RDV mardi 14h"),
proposer de l'ajouter à un calendrier local (CalDAV) ou une todo list.

**Mon contre-argument :** c'est sortir du scope d'un "gestionnaire
d'emails". On est déjà ambitieux avec 14 subagents et un système RAG.
Ajouter CalDAV c'est un autre produit. L'utilisateur peut copier-
coller une date dans son calendrier, ce n'est pas critique.

**Action : aucune, hors scope.**

### 2.5 "Recherche sémantique en langage naturel"

**L'avis dit :** remplacer la barre de recherche classique par
"Retrouve le mail où Marc parlait du budget Q3".

**Mon contre-argument :** c'est déjà dans le code
(`HybridSearch.fulltext_search()` existe). Il manque juste l'endpoint
et l'UI. Pas un angle mort, juste pas exposé.

**Action : ajouter `GET /api/search?q=...` dans une itération future.**

### 2.6 Mode "Éco" + quantisation Q4_K_M

**L'avis dit :** désactiver la sandbox, utiliser un modèle quantisé
uniquement quand le CPU est idle.

**Mon contre-argument :** Ollama applique déjà la quantisation par
défaut selon le modèle qu'on pull. Le bge-m3 est déjà très léger
(~2 Go en RAM). Optimiser la perf tant qu'on n'a pas mesuré le
bottleneck est prématuré. Une fois déployé, on profile et on voit.

**Action : aucune, à profiler plus tard.**

### 2.7 "Explicabilité via LLM"

**L'avis dit (implicitement) :** générer l'explication via le LLM.

**Mon contre-argument :** c'est over-engineering. Le `Rules Engine`
peut générer une explication déterministe et instantanée, sans
appel réseau. C'est plus rapide, plus testable, plus déterministe,
et l'utilisateur n'a pas besoin d'un paragraphe généré par un LLM
pour comprendre "même domaine que 12 newsletters archivées".

**Action : template déterministe (voir 1.1).**

---

## Partie 3 — Recommandation prioritaire de l'avis

L'avis recommande : **"Explicabilité + Correction Rapide"** comme LA
priorité avant P2.

**Je valide à 100 %.** C'est ce qui transforme un outil technique
en produit adopté. Un humain qui peut dire "OK, je comprends" et
corriger en un clic fait confiance. Un humain qui subit des décisions
opaques finit par désactiver.

L'implémentation peut être déterministe (pas besoin d'un LLM pour
expliquer), c'est même mieux.

---

## Partie 4 — Quick wins immédiats (si tu veux cette nuit)

| # | Quick win | Effort | Bénéfice |
|---|-----------|--------|----------|
| 1 | Explicabilité déterministe (`decision_rationale`) | ~25 min | Élevé |
| 2 | Règle anti-PJ non classées (sécurité) | ~15 min | Très élevé |
| 3 | Encodage MIME robuste (chardet) | ~20 min | Élevé |
| 4 | historyId fallback intelligent (sync_full 7j) | ~30 min | Élevé |

**Total : ~1h30, zéro régression sur l'existant.**

---

## Partie 5 — Ce que je recommande de NE PAS faire cette nuit

- Multi-account (gros chantier)
- Privacy audit dédié (produit en soi)
- CalDAV / todo list (hors scope)
- Mode Shadow RAG (utile mais post-P1)
- Quantisation / mode éco (à profiler plus tard)

---

## Conclusion

L'avis identifie **5 vrais angles morts** (1.1 à 1.5) et **7 points
discutables** (2.1 à 2.7). Les 5 premiers sont à traiter, idéalement
avant une première mise en production. Les 7 derniers sont soit déjà
couverts, soit hors scope, soit prématurés.

La recommandation prioritaire de l'avis (explicabilité + correction
rapide) est **validée et immédiatement actionnable**.

**Priorité opérationnelle recommandée :**
1. Quick wins 1, 2, 3 (~1h, peuvent être faits cette nuit)
2. historyId fallback (~30 min, peut être fait cette nuit)
3. Documentation utilisateur du système d'explication
4. Itération future : Shadow Mode, multi-account, etc.
