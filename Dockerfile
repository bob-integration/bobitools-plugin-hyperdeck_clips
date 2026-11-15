# SPDX-License-Identifier: GPL-3.0-or-later
# Image de l'outil « Clips HyperDeck ». Alpine plutôt que python:slim : ffmpeg y pèse
# ~190 Mo au total contre ~570 Mo sur Debian — l'hôte de production est un petit LXC.
# ffmpeg sert à trois choses : ffprobe (contrôle de format), le rendu des bouche-trous
# (drawtext → police DejaVu) et la correction d'en-tête de trame (copie sans réencodage).
# Le reste (FTP, TCP 9993, HTTP) : bibliothèque standard, aucune dépendance pip.
FROM alpine:latest
RUN apk add --no-cache python3 ffmpeg font-dejavu

WORKDIR /app
COPY server.py engine.py store.py media.py deck.py csvlist.py /app/

# Lecteurs, listes et historique ; dépôts en transit (incoming/) et bouche-trous en cours
# de rendu (work/), purgés au démarrage.
VOLUME ["/data"]

# DOIT correspondre à docker.port du plugin.json.
EXPOSE 8080
CMD ["python3", "-u", "/app/server.py"]
