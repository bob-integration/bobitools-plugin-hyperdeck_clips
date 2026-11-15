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

## Réponses FTP décalées
Sur HyperDeck réel, un renommage a reçu « 200 » au `RNFR` puis « 350 » au `RNTO` : les
réponses arrivent décalées d'un cran (350 est la réponse normale au RNFR). Le client FTP
de l'outil lit et écarte, avant chaque commande, toute réponse déjà arrivée sans avoir été
demandée, et le renommage reconnaît un 350 tardif. Le journal brut de l'échange accompagne
chaque refus ; « Tester la connexion » fait un renommage d'essai sur un petit fichier
texte (invisible dans la timeline) et montre cet échange.

## Espace du HyperDeck
Le protocole ne donne que le temps d'enregistrement restant. L'espace libre en est
déduit au débit du format d'enregistrement (`configuration` → `file format`, débits ProRes
et DNx publiés), d'où le « ≈ » ; capacité = libre estimé + taille des clips (LIST quand
le serveur n'a pas MLSD). Pas d'estimation pour un codec à débit variable (H.264/H.265).

## À vérifier sur une vraie machine
- Renommage FTP (`RNFR/RNTO`) : hypothèse du décalage à confirmer avec « Tester la connexion ».
- Prise en compte d'un fichier arrivé par FTP dans `clips get` (sans re-sélection du disque).
- Format exact des noms renvoyés par `clips get` (avec ou sans extension : les deux sont gérés).
- Encodage UTF-8 des noms accentués (`OPTS UTF8 ON`).

## Dépendance cœur
Les envois passent en `application/octet-stream` : il faut le relais par morceaux du cœur
(`app/routes.py:_proxy_stream`). Sans lui, le corps n'arrive pas au conteneur.
