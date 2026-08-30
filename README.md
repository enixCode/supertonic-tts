# supertonic-tts

Service TTS **local et dockerisé**, à embarquer dans n'importe quel projet via HTTP.
Moteur : le modèle officiel **Supertonic 3** (Supertone), 99M paramètres, inférence
CPU, 31 langues. Pas de clonage de voix, pas de service tiers, rien ne sort de la
machine.

> **Projet indépendant, sans aucun lien avec Supertone Inc.** Ce dépôt n'est ni
> édité, ni approuvé, ni soutenu par l'entreprise. Il se contente d'utiliser son
> SDK et son modèle publics. Pour le projet officiel, voir
> [supertone-inc/supertonic](https://github.com/supertone-inc/supertonic).

Le serveur HTTP est **celui du SDK Supertonic**, pas une réimplémentation. Ce dépôt
n'ajoute que ce qui lui manque : une route `/v1/audio/speech` **réellement
compatible OpenAI**, avec le streaming et les formats que le SDK ne couvre pas, et
la **fabrication de voix** par mélange d'embeddings (`blend.py`).

Concrètement, le client officiel `openai` fonctionne dessus sans une ligne
d'adaptation, en changeant seulement l'URL de base :

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="peu-importe")

with client.audio.speech.with_streaming_response.create(
        model="tts-1", voice="alloy", input="Bonjour.",
        response_format="mp3", stream_format="audio") as r:
    for chunk in r.iter_bytes():
        ...  # le premier son arrive apres la premiere phrase, pas a la fin
```

## Écouter

Deux extraits produits par ce service, tels quels, sans retouche :

**Preset officiel `M1` (Alex)**, en anglais, réglages par défaut :

<video src="https://github.com/user-attachments/assets/e16aeee0-b91b-46a8-a484-883209f4e236"></video>

**Voix custom `PRESENTER_FR`**, en français, `speed=1.18` :

<video src="https://github.com/user-attachments/assets/576c65df-6d09-43b7-9c38-b873311b8519"></video>

Le premier montre le service tel qu'il sort de la boîte, le second ce qu'on obtient
en fabriquant sa propre voix avec `blend.py`. Les mêmes extraits sont versionnés
dans [`demo/`](demo) : [`preset-en.mp4`](demo/preset-en.mp4) et
[`presenter-fr.mp4`](demo/presenter-fr.mp4).

<!-- Les deux src ci-dessus sont des assets GitHub, obtenus en deposant les .mp4
     dans l'editeur web d'une issue. C'est le SEUL moyen d'avoir un lecteur : une
     balise video pointant un fichier du depot (raw.githubusercontent.com) est
     supprimee par la sanitisation, verifie sur le README rendu. -->

Pour les régénérer, l'API lancée :

```bash
# 1. la synthese
curl -X POST http://localhost:8000/v1/tts -H "Content-Type: application/json" \
  -d '{"text":"Good evening. Here are tonight headlines, in thirty seconds, straight to the point. This voice is a factory preset, running entirely on your own machine.","voice":"M1","lang":"en","steps":16,"speed":1.0}' \
  -o demo/preset-en.wav

curl -X POST http://localhost:8000/v1/tts -H "Content-Type: application/json" \
  --data-binary @- <<'JSON' -o demo/presenter-fr.wav
{"text":"Bonjour, ici votre présentateur. Aujourd'hui, l'essentiel de l'actualité en trente secondes, sans langue de bois. Restez avec moi.","voice":"PRESENTER_FR","lang":"fr","steps":16,"speed":1.18}
JSON

# 2. le wav jetable devient le mp4 versionne (forme d'onde + audio)
for f in preset-en presenter-fr; do
  ffmpeg -y -i "demo/$f.wav" \
    -filter_complex "[0:a]showwaves=s=640x160:mode=cline:rate=12:colors=0x22d3ee,format=yuv420p[v]" \
    -map "[v]" -map 0:a -c:v libx264 -preset slow -crf 34 -c:a aac -b:a 96k -movflags +faststart \
    "demo/$f.mp4"
done
```

## Prérequis

Docker (conteneurs Linux). Rien d'autre : le SDK et le modèle vivent dans l'image et
dans un volume.

## Démarrer

```bash
docker compose build          # une fois
docker compose up -d api      # API sur http://localhost:8000
```

Ou sans rien construire, en tirant l'image publiée par la CI (`amd64` et `arm64`,
donc PC, serveur, Mac Apple Silicon et Raspberry Pi) :

```bash
docker run -d -p 127.0.0.1:8000:8000 \
  -v ./voices:/app/voices -v st-model-cache:/data \
  ghcr.io/enixcode/supertonic-tts:latest
```

> Au premier démarrage, le modèle `supertonic-3` (~404 Mo) se télécharge une fois
> dans le volume Docker `st-model-cache` (~60 s). Ensuite l'API répond direct.

Doc interactive sur **http://localhost:8000/docs**.

## Endpoints

| Méthode | Route | Origine | Description |
|---|---|---|---|
| `POST` | `/v1/audio/speech` | **ce dépôt** | **compatible OpenAI**, avec streaming et 4 formats |
| `POST` | `/v1/tts` | SDK | synthèse complète, un seul fichier en réponse |
| `POST` | `/v1/tts/batch` | SDK | jusqu'à 64 textes en une requête |
| `GET` | `/v1/styles` | SDK | presets officiels + voix importées |
| `POST` | `/v1/styles/import` | SDK | dépose un fichier de voix et le rend utilisable |
| `GET` | `/v1/health` | SDK | état, modèle, fréquence d'échantillonnage |

### Formats de sortie

`/v1/audio/speech` suit la spécification OpenAI : `response_format` vaut par défaut
**`mp3`**, comme chez OpenAI, et non `wav`.

| Format | Streamable | Remarque |
|---|---|---|
| `mp3` | oui | le défaut, frames concaténables |
| `wav` | oui | en-tête puis PCM au fil de l'eau |
| `pcm` | oui | PCM 16 bits brut, **44,1 kHz** et non 24 kHz comme chez OpenAI |
| `flac` | non | conteneur avec index : servi en un bloc, sans gain de latence |
| `opus` | refusé | le codec n'accepte que 8, 12, 16, 24 ou 48 kHz, le modèle sort du 44,1 kHz |
| `aac` | refusé | demanderait un encodeur absent de l'image |

Les deux formats refusés renvoient un **400** qui nomme les formats disponibles.
Attention au `pcm` : un client qui suppose 24 kHz jouera le son trop lentement.

Les routes `/v1/tts` et `/v1/tts/batch` restent celles du SDK et acceptent, elles,
`wav`, `flac` et `ogg`.

```bash
curl -X POST http://localhost:8000/v1/tts -H "Content-Type: application/json" \
  -d '{"text":"Hello there.","voice":"M1","lang":"en"}' -o out.wav
```

## Le streaming, et pourquoi il existe

La synthèse coûte environ 45 ms par caractère sur CPU. Sans streaming, un texte long
n'émet donc rien avant d'être entièrement synthétisé. Le champ `stream_format`
découpe le texte sur les fins de phrase (la fonction de découpage du SDK), synthétise
segment par segment et **émet chacun dès qu'il est prêt**.

Mesuré sur 815 caractères :

| Requête | Premier bloc | Flux complet |
|---|---|---|
| `mp3`, sans `stream_format` | 19,45 s | 19,45 s |
| `wav` + `stream_format=audio` | **5,88 s** | 17,79 s |
| `mp3` + `stream_format=audio` | **6,11 s** | 19,43 s |
| `pcm` + `stream_format=audio` | **5,89 s** | 17,67 s |
| `flac` + `stream_format=audio` | 18,03 s | 18,04 s, un seul bloc |

Le premier son arrive trois fois plus vite, pour un surcoût total négligeable. Et
comme la synthèse va plus vite que la lecture, une fois le premier segment reçu, le
son ne se coupe pas.

### Les deux modes

**`stream_format: "audio"`** renvoie l'audio en chunked transfer. C'est ce que
consomme `openai-python` via `with_streaming_response`, et ce que lit un lecteur :

```bash
curl -N -X POST http://localhost:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Premiere phrase. Deuxieme phrase.","voice":"alloy","response_format":"mp3","stream_format":"audio"}' \
  | ffplay -nodisp -autoexit -
```

**`stream_format: "sse"`** renvoie des Server-Sent Events, pour un client web qui
veut des évènements plutôt qu'un flux d'octets :

```
data: {"type":"speech.audio.delta","audio":"<base64>"}

data: {"type":"speech.audio.done","usage":{"input_tokens":203,...}}
```

Un `delta` par segment, puis un `done`. Le champ `usage` est une estimation à quatre
caractères par token : ce service ne compte pas de tokens, mais renvoyer un objet
vide casserait les clients qui lisent ce champ.

## Configuration

Deux variables valent pour tout le serveur, les autres ne changent que les défauts de
`/v1/audio/speech` : `/v1/tts` et `/v1/tts/batch` gardent ceux du SDK. Tout se
surcharge requête par requête. Bloc `environment:` à décommenter dans
`docker-compose.yml`.

| Variable | Défaut | Portée |
|---|---|---|
| `TTS_MODEL` | `supertonic-3` | tout le serveur |
| `TTS_VOICES_DIR` | `voices` | tout le serveur |
| `TTS_VOICE` | `M1` | `/v1/audio/speech` |
| `TTS_LANG` | `en` | `/v1/audio/speech` |
| `TTS_SPEED` | `1.05`, défaut du SDK | `/v1/audio/speech` |
| `TTS_STEPS` | `8`, défaut du SDK | `/v1/audio/speech` |
| `TTS_MAX_CHUNK_LENGTH` | `300` | `/v1/audio/speech` |
| `TTS_SILENCE_DURATION` | `0.3` | `/v1/audio/speech` |

### Noms de voix

Les dix voix OpenAI sont mappées sur les dix presets Supertonic, pour qu'un client
qui demande `alloy` obtienne quelque chose plutôt qu'un 404 :

| OpenAI | alloy | ash | ballad | echo | verse | coral | sage | shimmer | marin | cedar |
|---|---|---|---|---|---|---|---|---|---|---|
| Supertonic | M1 | M2 | M3 | M4 | M5 | F1 | F2 | F3 | F4 | F5 |

C'est un alias de commodité, pas une ressemblance de timbre. Les noms natifs
(`M1`..`F5`) et ceux des voix importées fonctionnent aussi.

## Fabriquer une voix custom

C'est le second apport de ce dépôt : le SDK ne sait pas mélanger des voix.

Un fichier de voix Supertonic n'est pas un JSON de réglages, c'est **deux embeddings
appris, indépendants** :

- `style_ttl` `[1,50,256]` → le **timbre** (la couleur de la voix)
- `style_dp` `[1,8,16]` → le **rythme** (la durée des mots ; c'est lui qui fait qu'un
  mot traîne ou claque)

Le prédicteur de durée n'utilise que `style_dp`, et `speed` divise ensuite toutes les
durées. On peut donc doser timbre et rythme **séparément**, et marier par exemple un
timbre grave à un débit vif. Mélanger des embeddings valides reste dans la
distribution du modèle.

```bash
# timbre grave (Alex + Daniel) mais rythme plus vif (celui d'Alex seul)
docker compose run --rm api python blend.py M1:0.5 M5:0.5 --dp M1:1.0 -o voices/ma-voix.json
```

La voix apparaît aussitôt dans `GET /v1/styles` et s'utilise via `{"voice":"ma-voix"}`.

Presets et noms officiels (démo Supertone) : M1 Alex, M2 James, M3 Robert, M4 Sam,
M5 Daniel, F1 Sarah, F2 Lily, F3 Jessica, F4 Olivia, F5 Emily.

### Déposer une voix déjà fabriquée

Si le fichier vient d'ailleurs, `POST /v1/styles/import` l'installe sans toucher au
conteneur. Le SDK valide la structure et plafonne la taille avant d'écrire.

```bash
curl -X POST http://localhost:8000/v1/styles/import \
  -F name=MON_PRESENTATEUR \
  -F file=@voices/ma-voix.json
```

Le formulaire existe aussi dans le Swagger, avec un sélecteur de fichier.

## Sécurité

Deux mesures sont en place par défaut.

**Le service n'écoute que sur cette machine.** Le compose publie sur
`127.0.0.1:8000`, pas sur `0.0.0.0`. Pour l'ouvrir au réseau local, remplacer par
`"8000:8000"`. À faire en connaissance de cause : **aucune authentification n'est
prévue**, et une seule requête peut monopoliser le CPU longtemps (le SDK accepte
jusqu'à 100 000 caractères, soit plus d'une heure de calcul).

**Le conteneur ne tourne pas en root.** Le Dockerfile crée un utilisateur `app`
(uid 10001) qui possède `/data` et `/app`. Un volume de modèle créé avant cette
version doit lui être donné une fois :

```bash
docker run --rm -v supertonic-tts_st-model-cache:/data alpine chown -R 10001:10001 /data
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `api.py` | le serveur du SDK, plus l'endpoint de streaming |
| `LICENSE` | MIT, pour le code de ce dépôt uniquement |
| `.github/workflows/build.yml` | construit l'image `amd64` et `arm64` et la publie sur ghcr.io |
| `blend.py` | fabrique une voix custom (presets pondérés, `--dp` pour le rythme) |
| `voices/` | les voix custom (`<nom>.json`) |
| `demo/` | les deux extraits du README, en `.mp4` |

Pour les besoins ponctuels en ligne de commande, le SDK installe sa propre commande
dans l'image : `supertonic say`, `supertonic tts`, `supertonic list-voices`,
`supertonic info`.

```bash
docker compose run --rm api supertonic tts "Bonjour tout le monde." \
  -o voices/out.wav --voice M5 --lang fr
```

## Non-affiliation

Ce projet est **indépendant**. Il n'est ni édité, ni approuvé, ni soutenu, ni
sponsorisé par **Supertone Inc.**, et n'entretient aucune relation avec cette
entreprise. « Supertone » et « Supertonic » sont des marques de leurs détenteurs
respectifs, citées ici uniquement pour désigner le SDK et le modèle utilisés.

C'est d'ailleurs une obligation de la licence du modèle, dont l'article 8 précise
que rien n'autorise à faire usage des marques du concédant ni à suggérer une
approbation de sa part.

Les ressources officielles, à consulter en priorité :

| Ressource | Lien |
|---|---|
| Projet Supertonic | [supertone-inc/supertonic](https://github.com/supertone-inc/supertonic) |
| SDK Python | [supertone-inc/supertonic-py](https://github.com/supertone-inc/supertonic-py) |
| Documentation du SDK | [supertone-inc.github.io/supertonic-py](https://supertone-inc.github.io/supertonic-py) |
| Modèle et poids | [huggingface.co/Supertone/supertonic-3](https://huggingface.co/Supertone/supertonic-3) |
| Démo en ligne | [huggingface.co/spaces/Supertone/supertonic-3](https://huggingface.co/spaces/Supertone/supertonic-3) |

## Licences et attributions

Ce dépôt combine trois choses qui n'ont pas la même licence.

| Élément | Licence | Détenteur |
|---|---|---|
| Le code de ce dépôt (`api.py`, `blend.py`, Docker) | **MIT**, voir [`LICENSE`](LICENSE) | le dépôt |
| Le SDK [`supertonic`](https://github.com/supertone-inc/supertonic-py) 1.3.1 | **MIT** | © 2025 Supertone Inc. |
| Les poids [`Supertonic/supertonic-3`](https://huggingface.co/Supertone/supertonic-3) et les voix preset | **BigScience Open RAIL-M** (18 août 2022) | Supertone Inc. |

### Ce qu'implique la licence des poids

Open RAIL-M n'est pas une licence permissive classique. Elle autorise l'usage, y
compris commercial, et la redistribution, mais elle attache au modèle des
**restrictions d'usage** (Attachment A) qui suivent toute copie et tout dérivé. Deux
obligations concrètes :

- Vous devez répercuter ces mêmes restrictions à quiconque utilise le modèle via
  votre service (article 5 de la licence).
- Toute redistribution doit être accompagnée du texte de la licence.

Parmi les usages interdits, ceux qui concernent directement un TTS : usurper
l'identité de quelqu'un sans son consentement (deepfakes, alinéa g), diffuser du
contenu généré **sans indiquer clairement qu'il est produit par une machine**
(alinéa e), diffuser de fausses informations dans le but de nuire (alinéa c),
harceler ou diffamer (alinéa f). La liste complète fait treize alinéas.

### Les audios que vous produisez

L'article 6 est explicite : *« Licensor claims no rights in the Output You generate
using the Model »*. Les fichiers audio générés vous appartiennent, vous en êtes
responsable, et leur usage reste soumis aux restrictions ci-dessus.

### La voix fournie

`voices/PRESENTER_FR.json` est une combinaison pondérée d'embeddings de voix
officielles Supertone. C'est un dérivé des poids, donc il relève de la même licence
Open RAIL-M, pas de la licence du code.

> Ce résumé est une lecture des textes livrés avec le paquet et avec le modèle
> (`LICENSE` du SDK, `LICENSE` du dépôt Hugging Face). Il ne remplace pas leur
> lecture intégrale, ni un avis juridique.
