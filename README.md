# Moniteur de stock Midea PortaSplit

Cet outil surveille le **Midea PortaSplit 12 000 BTU**, modèle
`MMCS-12HRN8-QRD0`, chez :

- Leroy Merlin
- Boulanger
- Darty
- ManoMano
- Optimea

**Castorama est volontairement exclu.**

Il ouvre les pages avec un vrai navigateur Chromium, détecte une disponibilité
ou une nouvelle information de réapprovisionnement, puis envoie un e-mail.
L'état précédent est conservé afin de ne pas répéter la même alerte.

## Option recommandée : GitHub Actions

Le fichier `.github/workflows/monitor.yml` lance la vérification toutes les
15 minutes. GitHub peut toutefois retarder ponctuellement un lancement
planifié de quelques minutes.

### 1. Créer un dépôt GitHub privé

1. Créez un nouveau dépôt privé sur GitHub.
2. Décompressez ce dossier et envoyez tous les fichiers dans le dépôt.
3. Ouvrez l'onglet **Actions** du dépôt et autorisez les workflows si GitHub le demande.

### 2. Préparer l'adresse d'envoi

Avec Gmail, activez la validation en deux étapes puis créez un **mot de passe
d'application** Google. N'utilisez pas votre mot de passe Gmail habituel.

Dans le dépôt GitHub, ouvrez :

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Ajoutez les secrets suivants :

| Secret | Valeur Gmail habituelle |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | l'adresse Gmail qui envoie |
| `SMTP_PASSWORD` | le mot de passe d'application à 16 caractères |
| `ALERT_TO` | l'adresse qui doit recevoir l'alerte |
| `ALERT_FROM` | facultatif : l'adresse Gmail qui envoie |
| `SMTP_SSL` | `false` |

Pour Outlook, utilisez généralement `smtp.office365.com`, port `587`.

### 3. Faire un test

Dans GitHub :

1. `Actions`
2. `Surveillance Midea PortaSplit`
3. `Run workflow`

La première exécution enregistre seulement l'état actuel afin d'éviter cinq
fausses alertes initiales. Pour tester immédiatement l'e-mail en local :

```bash
python portasplit_monitor.py --test-email
```

Pour recevoir un résumé forcé de l'état courant :

```bash
python portasplit_monitor.py --force-alert
```

## Exécution locale sous Windows 11

Installez Python 3.12, puis dans PowerShell :

```powershell
pip install -r requirements.txt
playwright install chromium
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="votre-adresse@gmail.com"
$env:SMTP_PASSWORD="mot-de-passe-application"
$env:ALERT_TO="adresse-d-alerte@example.com"
python .\portasplit_monitor.py --test-email
python .\portasplit_monitor.py
```

Vous pouvez ensuite appeler le script avec le Planificateur de tâches Windows.

## Limites importantes

- Les enseignes modifient parfois leur site ou bloquent les navigateurs
  automatisés. Les règles se trouvent dans `sites.json` et sont modifiables.
- La disponibilité **locale** Leroy Merlin dépend du magasin, du code postal et
  parfois de cookies. L'outil détecte surtout le stock commandable en ligne.
  Il peut ne pas voir une unique unité physiquement disponible à La Valentine
  ou Plan-de-Campagne.
- Un bouton « Ajouter au panier » peut rester affiché malgré une rupture :
  l'outil donne donc priorité aux mentions explicites telles que
  « Produit épuisé », « Rupture de stock » ou « Livraison indisponible ».
- Une alerte reste une invitation à vérifier rapidement la page avant paiement.
- Le rythme de 15 minutes évite de solliciter excessivement les sites tout en
  restant utile pour un produit qui se vend vite.

## Ajouter ou retirer une enseigne

Modifiez `sites.json`. Chaque entrée contient :

- l'URL à vérifier ;
- les expressions positives ;
- les expressions négatives ;
- les mentions de précommande ;
- le nombre minimal d'indices positifs exigés.

Passez `"enabled": false` pour désactiver temporairement une enseigne.
