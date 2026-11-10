# A3C — Référentiel des barèmes Bâtiment Hauts-de-France

Ce dépôt sert **uniquement** à diffuser les barèmes contrôlés à destination du simulateur **local** A3C. Le simulateur ne doit **pas** être hébergé ici. Le dépôt est public et ne reçoit aucune donnée d'entreprise ou de salarié.

## Publication initiale sur GitHub (responsable A3C)

1. Connectez-vous sur https://github.com et ouvrez **+ → New repository**.
2. Nom du dépôt : `a3c-baremes-batiment-hdf`. Visibilité : **Public**. Ne cochez pas la création automatique du README, de la licence ou d'un `.gitignore` (ils sont dans le kit). Cliquez sur **Create repository**.
3. Décompressez le ZIP livré par A3C. Dans le dépôt vide, cliquez sur **uploading an existing file** (ou **Add file → Upload files**) et déposez les **éléments à l'intérieur du dossier**, pas le fichier ZIP. Il faut notamment conserver le chemin `.github/workflows/publication.yml`. Cliquez sur **Commit changes**.
4. Ouvrez **Settings → Pages → Build and deployment → Source**, puis choisissez **GitHub Actions**. Ce kit dispose de son propre flux de publication avec validation préalable du format des données.
5. Dans **Actions**, sélectionnez « Contrôler et publier le référentiel A3C », puis **Run workflow** si aucune exécution ne s'est déclenchée. Autorisez les Actions pour le dépôt si GitHub le demande.
6. Après publication, l'adresse du fichier sera `https://VOTRE-COMPTE.github.io/a3c-baremes-batiment-hdf/baremes.json`. **VOTRE-COMPTE** est un exemple, à remplacer par le nom du propriétaire GitHub réel. Ouvrez cette adresse et vérifiez qu'elle affiche un JSON.
7. Dans le simulateur local **V14**, ouvrez « Réglage du référentiel distant », collez l'adresse HTTPS exacte et cliquez sur **Enregistrer**. Une connexion Internet permet ensuite la consultation du fichier à chaque ouverture.

> **Situation initiale :** `records: []` est volontaire. La connexion peut être opérationnelle sans qu'aucun nouveau barème ne soit diffusé ; les barèmes 2024–2026 demeurent dans le fichier HTML local.

## Ajout d'un nouvel accord

Pour un accord nouveau, un responsable A3C doit vérifier le texte officiel Légifrance (territoire, IDCC, dates d'effet et d'extension, montants repas / transport / trajet pour chacune des cinq zones). Ajoutez ensuite un nouvel objet au tableau `records` du fichier `baremes.json` ; ne modifiez pas les barèmes historiques déjà embarqués dans le simulateur.

Le format de chaque entrée est :

```json
{
  "id": "identifiant-unique",
  "libelle": "Intitulé exact de l'accord contrôlé",
  "debut": "AAAA-MM-JJ",
  "extension": "AAAA-MM-JJ",
  "repas": 0,
  "transport": [0, 0, 0, 0, 0],
  "trajet": [0, 0, 0, 0, 0],
  "source": "https://www.legifrance.gouv.fr/..."
}
```

**Ce modèle comporte des zéros fictifs et n'est surtout pas à publier comme un vrai barème.** Il illustre uniquement la structure des champs. Le validateur bloque les données mal formées, **pas** les erreurs de fond juridique : la vérification humaine est indispensable.

L'action GitHub « Contrôler et publier le référentiel A3C » se lance à la modification du fichier. **Elle ne publie que si le contrôle technique réussit.** En cas d'échec, la dernière version en ligne reste publiée.

## Mise à jour passive côté employeur

Une fois le flux GitHub Pages configuré dans le HTML, l'employeur n'a plus à modifier le fichier ni à importer de barèmes : les nouvelles versions **publiées et validées** sont relues automatiquement à l'ouverture. En l'absence de réseau, le dernier référentiel enregistré localement est conservé (selon les permissions du navigateur). Aucun contrôle officiel ne se produit chez l'artisan : la validation des accords est de la responsabilité de l'éditeur du flux.

## Ce que le dispositif ne fait pas encore

Il n'extrait **pas automatiquement** les nouveaux montants conventionnels depuis la DILA/KALI ou Légifrance. Une veille automatisée des publications peut être ajoutée ultérieurement ; elle ne suffira pas, à elle seule, à certifier l'opposabilité des accords. La présence d'un dépôt GitHub ne transforme pas la mise à jour passive des données en production automatique de nouveaux barèmes.

## Références

- Légifrance (article 8-14) : https://www.legifrance.gouv.fr/conv_coll/article/KALIARTI000005778335
- KALI (DILA) : https://www.data.gouv.fr/datasets/kali-conventions-collectives-nationales
- Documentation GitHub Pages : https://docs.github.com/fr/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site
