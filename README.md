# Clips HyperDeck — plugin Bobi.Tools

Livraison de clips sur des lecteurs **Blackmagic HyperDeck** dont l'exploitant rappelle les
clips **par leur rang** (clip 1, clip 2…), pour
[Bobi.Tools](https://github.com/bob-integration/bobi.tools). Les fichiers arrivent par le FTP
de la machine ; ce que la machine en voit est relu par le *HyperDeck Ethernet Protocol*
(TCP 9993). Une même liste peut servir plusieurs lecteurs.

Le HyperDeck classe ses clips par **ordre alphabétique de nom de fichier**. Rappeler un clip
par son rang n'est fiable que si la liste garde toujours le même nombre de fichiers, dans le
même ordre : tout l'outil est construit autour de cette règle.

## Ce que fait l'outil

- **Liste attendue** par lecteur, importée d'un CSV (`ID;Player;Catégorie;Numéro;Nom` ;
  seules les colonnes Numéro et Nom sont obligatoires), exportable. Un **emplacement** par
  ligne, nommé sur la machine `<Numéro> - <nom du fichier déposé>`.
- **Bouche-trous** : chaque emplacement sans clip porte un fichier au format du lecteur
  (`<Numéro> - VIDE - <Nom>` par défaut), pour que les rangs ne bougent jamais. Fixe, animé
  (pour voir que la machine lit) ou transparent en ProRes 4444 (sortie fill & key) — les
  trois validés sur un vrai HyperDeck.
- **Livrer** par glisser-déposer, un fichier ou un lot (réparti selon le numéro en tête du
  nom, ou à la suite). Le format est contrôlé **avant** l'envoi ; un clip remplacé l'est sans
  décaler les autres ; un clip en cours de lecture n'est pas retiré avant la fin (« Forcer »
  possible).
- **Déplacer / échanger** deux emplacements, **combler** les vides, **fin de tournage** (tout
  remettre en bouche-trous, en option supprimer les fichiers hors liste).
- **Écart décrit, jamais masqué** : ce que la machine liste est comparé à la liste attendue,
  rang par rang ; un écart dit sa cause et le geste qui le résout.

## À savoir

- **Le FTP du HyperDeck ne renomme pas** (relevé sur Studio HD Pro : le `RNFR` reçoit
  « 350 » puis « 550 »). Toute livraison est donc un envoi complet ; un déplacement relit
  le fichier puis le renvoie. Pendant un envoi, un emplacement peut porter brièvement deux
  fichiers.
- **La machine relit son disque en différé** après un échange FTP : pendant environ une
  minute et demie, un écart entre le FTP et la liste du HyperDeck est normal. L'outil
  attend avant de conclure.
- **Noms livrés en ASCII.** Un nom avec un accent tapé sur Mac (forme décomposée) est
  accepté par le FTP, lu par la machine, mais invisible au FTP et impossible à supprimer à
  distance : un « fichier fantôme » qui décale tous les rangs. L'outil retire les accents
  avant l'envoi ; un fantôme déjà présent est annoncé en rouge « ACTION MANUELLE REQUISE »
  (carte à vider sur un ordinateur).
- **Ordre des trames lu dans les images.** Chaque image ProRes dit quelle trame vient en
  premier ; c'est elle qui tranche, pas l'étiquette du conteneur. Le HyperDeck étiquette
  lui-même la trame haute `fiel 02 09` (« tb » pour ffprobe), comme Apple ProRes : cette
  étiquette est normale. Un fichier exporté en trame basse est refusé.
- **Un seul format par lecteur** : le HyperDeck ne liste ensemble que des clips du même
  format. Changer le format d'un lecteur propose de tout refaire au nouveau format.
- **Place** : la carte du HyperDeck (octets exacts annoncés par `slot info`) et le disque du
  serveur (chaque clip y transite en entier) sont vérifiés avant d'accepter un fichier.
- **Droits** : livrer (`clips.deliver`), remettre à zéro (`clips.reset`), gérer les lecteurs
  et leurs listes (`clips.manage`) ; restreignables par lecteur (Réglages → Outils →
  Périmètres).

## Pas encore vérifié sur une vraie machine

- Suppression d'un fichier fantôme par le réseau (`DELE` sous toutes les écritures du nom,
  retrait provisoire de la timeline par `clips remove`).
- Refus « carte pleine » au moment de l'envoi.

## Prérequis

- **Bobi.Tools** avec **Docker** : l'outil tourne en conteneur (`runtime: docker`, Alpine +
  ffmpeg). Le cœur doit relayer les envois de fichiers par morceaux (commit « Proxy outils
  Docker : relais par morceaux », 2026-11) : sans lui, les fichiers arrivent vides et l'outil
  répond « cœur trop ancien ».
- Des HyperDeck joignables en **FTP** (port 21) et sur **TCP 9993**, réglés au format des
  clips à livrer.
- De la place sur le serveur : chaque clip y est stocké en entier avant l'envoi.

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer », ou import du paquet
`.bobitool` (Réglages → Outils). Le dépôt est **privé** : l'instance doit avoir accès à
l'organisation GitHub.

L'aide complète est dans [`help.md`](help.md), affichée dans Bobi.Tools (menu « ? » → Aide).
