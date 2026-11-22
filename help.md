# Clips HyperDeck — aide

L'outil livre des clips sur des lecteurs HyperDeck dont l'exploitant rappelle les clips
**par leur rang** (clip 1, clip 2…). Le HyperDeck classe ses clips par **ordre alphabétique
de nom de fichier** : pour qu'un rang désigne toujours le même clip, la carte doit toujours
porter le même nombre de fichiers, dans le même ordre. L'outil s'en charge — à condition que
tous les fichiers passent par lui.

## Mettre en place un lecteur

1. **+ Lecteur**, puis renseignez le nom, l'**adresse du HyperDeck**, l'accès **FTP**
   (identifiant et mot de passe s'il y en a) et le **disque** (bouton « Parcourir… »).
2. Réglez le **format des clips** exactement comme celui du HyperDeck (codec, image, images
   par seconde, balayage). Le HyperDeck ne lit ensemble que des clips du même format.
3. **Tester la connexion** vérifie le FTP et le protocole réseau de la machine.
4. **Importer la liste (CSV)**, puis **Combler les vides** : chaque emplacement reçoit un
   bouche-trou, et la carte porte dès lors tous ses rangs.

## La liste (CSV)

- Colonnes `ID;Player;Catégorie;Numéro;Nom` (séparateur `;`, `,` ou tabulation). Seules
  **Numéro** et **Nom** sont obligatoires ; Catégorie regroupe les emplacements à l'écran.
- **Tous les numéros de la même longueur** (101, 102… 999, pas 99 et 100) : sinon l'ordre
  alphabétique ne suit plus l'ordre des numéros. Un numéro en double est refusé.
- « Télécharger un CSV exemple » donne un modèle ; « Exporter la liste actuelle » la
  ressort telle quelle.

## Livrer un clip

- **Glissez le fichier sur son emplacement.** Il part sur la carte sous le nom
  `<Numéro> - <nom du fichier>`, accents retirés.
- **Plusieurs fichiers d'un coup** : lâchés sur une case, ils remplissent les cases
  suivantes ; lâchés ailleurs, ils sont répartis selon le numéro en tête de leur nom
  (« 104 - … »). Un aperçu montre où chacun ira avant de valider.
- **Le format est contrôlé avant l'envoi.** Un fichier dans un autre format est refusé,
  avec ce qui ne va pas et ce qui est attendu.
- **Remplacer** un clip livré (V2, V3…) ne décale pas les autres. Si le clip est **à
  l'antenne**, l'échange attend la fin de sa lecture (« Attente : clip à l'antenne ») ;
  « Forcer » l'impose quand même.

### Ordre des trames (entrelacé)

L'outil lit l'ordre des trames **dans les images** du fichier, pas dans son étiquette.

- **« Étiquette de trame à corriger »** : les images sont dans le bon ordre, seule
  l'étiquette du fichier dit autre chose. Acceptez : l'étiquette est réécrite comme
  l'écrit le HyperDeck, sans réencoder ; l'image n'est pas touchée.
- **Refus « ordre des trames »** : le fichier a été exporté en **trame basse (inférieure)
  en premier**. Refaites l'export en **trame haute (supérieure) en premier** — corriger
  l'étiquette ferait trembler l'image à l'antenne.

## Lire l'état

En haut, le bandeau dit si **le HyperDeck voit la liste attendue**, rang par rang.

- **⟳ pas encore pris en compte** : normal juste après un envoi. Le HyperDeck relit sa
  carte en différé (jusqu'à environ une minute et demie) ; l'outil revérifie seul.
- **⚠ ne voit pas la liste attendue** : l'écart est décrit (rangs touchés, cause) avec le
  geste qui le résout.

États d'un emplacement :

| État | Ce que ça veut dire |
|---|---|
| Livré | le clip est sur la carte, à son rang |
| Vide · bouche-trou | pas de clip, le bouche-trou tient le rang |
| MANQUANT | aucun fichier pour ce numéro : tous les rangs suivants sont décalés → « Combler les vides » |
| DOUBLON | plusieurs fichiers pour ce numéro : rangs décalés → en garder un |
| Bouche-trou périmé | le nom de l'emplacement a changé dans la liste → « Combler les vides » le refait |
| Fichier non suivi | un fichier posé hors de l'outil (client FTP…) porte ce numéro |

## ⚠ Action manuelle requise

Quand le titre l'annonce en rouge, l'outil a trouvé un fichier qu'il **ne peut pas
supprimer** par le réseau : typiquement un nom avec un accent tapé sur Mac, que le
HyperDeck lit mais que son FTP ne voit pas. Il décale tous les rangs. Le cadre rouge donne
le nom du fichier et les étapes :

- retirer la carte, l'insérer dans un ordinateur et supprimer ce fichier ;
- ou, si la carte ne porte que des bouche-trous, la formater depuis le HyperDeck ;
- puis **Combler les vides**, et relivrer les clips concernés.

L'outil retire désormais les accents avant tout envoi : ce cas ne vient que de fichiers
posés par un autre moyen.

## Autres gestes

- **Déplacer** un clip vers un autre emplacement (ou l'échanger avec celui qui s'y trouve)
  et **Retirer** un clip (son bouche-trou revient) : depuis le panneau Détails.
- **Bouche-trous** (Réglages) : nom des fichiers, durée, pistes audio, style **fixe**,
  **animé** (pour voir que la machine lit) ou **transparent** (ProRes 4444, sortie fill &
  key).
- **Fin de tournage…** remet tous les emplacements en bouche-trous et, en option, supprime
  les fichiers hors liste. Il faut taper le nom du lecteur pour confirmer.
- **Changer le format** d'un lecteur propose aussitôt de tout refaire au nouveau format :
  des clips de l'ancien format resteraient invisibles.

## Place

- **Carte du HyperDeck** : un fichier qui ne tient pas (en comptant les envois déjà en file
  et une réserve) est refusé avant d'être envoyé.
- **Disque du serveur** : chaque clip y transite en entier avant d'aller au HyperDeck ; s'il
  manque de place, l'envoi est refusé.

## Droits

- **Livrer des clips** : déposer, remplacer, retirer, combler les vides.
- **Remettre à zéro en fin de tournage**.
- **Gérer les lecteurs** : ajouter, régler, supprimer un lecteur, importer sa liste,
  supprimer un fichier étranger.

Chacun peut être restreint lecteur par lecteur (Réglages → Outils → Périmètres).
