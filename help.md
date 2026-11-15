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

## À vérifier sur une vraie machine
- Renommage FTP (`RNFR/RNTO`) accepté par le HyperDeck.
- Prise en compte d'un fichier arrivé par FTP dans `clips get` (sans re-sélection du disque).
- Format exact des noms renvoyés par `clips get` (avec ou sans extension : les deux sont gérés).
- Encodage UTF-8 des noms accentués (`OPTS UTF8 ON`).

## Dépendance cœur
Les envois passent en `application/octet-stream` : il faut le relais par morceaux du cœur
(`app/routes.py:_proxy_stream`). Sans lui, le corps n'arrive pas au conteneur.
