# Clips HyperDeck (`hyperdeck_clips`)

Livraison de clips sur des HyperDeck Blackmagic dont l'exploitant rappelle les clips **par
leur rang**. Le HyperDeck classe ses clips par ordre alphabétique de nom de fichier : la
liste doit donc toujours compter le même nombre de clips, dans le même ordre.

## Principe
- Un **lecteur** = un HyperDeck (adresse, FTP, disque) + une **liste attendue** (CSV
  `ID;Player;Catégorie;Numéro;Nom`, seules Numéro et Nom sont obligatoires).
- Un **emplacement** par ligne. Nom sur la machine : `<Numéro> - <nom d'origine>`
  (`104 - Interview Dupont v2.mov`) ; emplacement vide : `<Numéro> - VIDE - <Nom>.mov`.
- Numéros de même largeur obligatoires (sinon l'ordre alphabétique ≠ l'ordre numérique).

## Échange sans décalage
1. envoi FTP sous `zzz_envoi_<id>.mov` (classé en fin de liste) ;
2. si le HyperDeck lit le clip à retirer : attente (bouton « Forcer » possible) ;
3. `DELE` de l'ancien puis `RNFR/RNTO` du temporaire ; repli par `STOR` direct si le
   renommage est refusé.

## Renommage FTP refusé (HyperDeck Studio HD Pro, relevé le 2026-11-15)
Le RNFR reçoit DEUX réponses : « 350 » puis « 550 File unavailable », pour un fichier
présent ; aucun renommage n'a lieu. L'outil lit toutes les réponses du RNFR, essaie le
chemin absolu, puis retient que le lecteur ne renomme pas et passe par des envois
complets : livraison = envoi direct puis suppression de l'ancien ; déplacer/échanger =
relecture (RETR) puis renvoi ; nom des bouche-trous = refabrication. Pendant un envoi,
l'emplacement peut porter deux fichiers (contrepartie inévitable sans renommage).
« Tester la connexion » refait l'essai et montre l'échange brut.

## Bouche-trous animés et transparents (0.9.0)
- **Animé** : une roue (un point avance d'un cran par seconde) et un compteur des secondes,
  pour voir que le HyperDeck lit. Tout change une fois par seconde : on n'encode qu'une
  image par seconde (segment muxer, 1 image par fichier) et la liste de concaténation la
  répète autant de fois qu'elle a d'images (cadences fractionnaires comprises), par copie
  de flux. Coût mesuré : 1,2 s au lieu de 0,7 s pour 10 s en 1080i50.
- **Transparent** (ProRes 4444 seulement, sortie fill & key) : fond transparent, cartouche
  de la couleur de fond autour des infos. Le cadre est MESURÉ par ffmpeg (filtre `bbox` sur
  un rendu blanc sur noir, compteur « 88 »), plus une marge. `drawbox` exige `replace=1`
  pour écrire l'alpha ; les gris semi-transparents sont pré-mélangés au fond (sinon ils
  perceraient la découpe). Poids : ≈ 145 Mo pour 10 s (c'est le 4444, pas l'alpha).
- Ces options n'entrent dans l'empreinte des bouche-trous que cochées : les bouche-trous
  déjà posés ne passent pas « périmés » à la mise à jour.

## Espace du HyperDeck
Le protocole ne donne que le temps d'enregistrement restant. L'espace libre en est
déduit au débit du format d'enregistrement (`configuration` → `file format`, débits ProRes
et DNx publiés), d'où le « ≈ » ; capacité = libre estimé + taille des clips (LIST quand
le serveur n'a pas MLSD). Pas d'estimation pour un codec à débit variable (H.264/H.265).

## À vérifier sur une vraie machine
- Le HyperDeck liste-t-il un fichier EN COURS d'envoi (décalage des rangs pendant l'envoi) ?
- Prise en compte d'un fichier arrivé par FTP dans `clips get` (sans re-sélection du disque).
- Format exact des noms renvoyés par `clips get` (avec ou sans extension : les deux sont gérés).
- Encodage UTF-8 des noms accentués (`OPTS UTF8 ON`).
- Lecture d'un bouche-trou animé (roue régulière, compteur) et découpe d'un bouche-trou
  ProRes 4444 transparent en sortie fill & key.

## Dépendance cœur
Les envois passent en `application/octet-stream` : il faut le relais par morceaux du cœur
(`app/routes.py:_proxy_stream`). Sans lui, le corps n'arrive pas au conteneur.
