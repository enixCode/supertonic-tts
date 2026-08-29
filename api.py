#!/usr/bin/env python3
"""Service HTTP supertonic-tts.

Le serveur vient du SDK (`supertonic.server`) : il fournit /v1/health,
/v1/styles, /v1/styles/import, /v1/tts et /v1/tts/batch. Ce module ne fait
qu'une chose : remplacer sa route /v1/audio/speech par une implementation
conforme a la specification OpenAI, que le SDK ne couvre que partiellement.

Ce que le SDK ne faisait pas et qui est ajoute ici :
  - les formats mp3 (le defaut d'OpenAI) et pcm, en plus de wav et flac, tous
    produits par soundfile, deja installe. opus et aac restent refuses, en 400
    explicite : voir UNSUPPORTED_FORMATS pour la raison de chacun.
  - `stream_format` : "audio" (chunked, ce que consomme openai-python) et "sse"
    (evenements speech.audio.delta / speech.audio.done, pour les clients web).
  - l'acceptation d'un `model` quelconque et des noms de voix OpenAI, sans quoi
    aucun client standard ne fonctionne tel quel.

Interet du streaming : la synthese coute ~45 ms par caractere. Sur 1600
caracteres, le premier son part en 6 s au lieu de 34 s.
"""
import base64
import io
import json
import os
import struct
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from supertonic.config import (DEFAULT_LANGUAGE, DEFAULT_MAX_CHUNK_LENGTH,
                               DEFAULT_SILENCE_DURATION, DEFAULT_SPEED,
                               DEFAULT_TOTAL_STEPS, MAX_SPEED, MIN_SPEED)
from supertonic.server import ServerState, create_app
# Fonction privee du SDK, assumee : la version est epinglee (supertonic==1.3.1)
# et la reimplementer ferait diverger la resolution de voix et ses erreurs.
from supertonic.server.routes import UnknownVoice, _resolve_voice
from supertonic.utils import chunk_text

VOICES_DIR = Path(os.getenv("TTS_VOICES_DIR", "voices"))

# Defauts repris du SDK, pour que la meme requete rende le meme audio quelle que
# soit la route appelee. Surchargeables par l'environnement.
STREAM_VOICE = os.getenv("TTS_VOICE", "M1")
STREAM_SPEED = float(os.getenv("TTS_SPEED", DEFAULT_SPEED))
STREAM_STEPS = int(os.getenv("TTS_STEPS", DEFAULT_TOTAL_STEPS))
STREAM_LANG = os.getenv("TTS_LANG", DEFAULT_LANGUAGE)
STREAM_MAX_CHUNK = int(os.getenv("TTS_MAX_CHUNK_LENGTH", DEFAULT_MAX_CHUNK_LENGTH))
STREAM_SILENCE = float(os.getenv("TTS_SILENCE_DURATION", DEFAULT_SILENCE_DURATION))

# response_format OpenAI -> (format soundfile, sous-type, type MIME, streamable)
# "streamable" = les octets de deux segments encodes separement peuvent etre
# concatenes tels quels. Vrai pour le PCM brut et pour les frames MP3, faux pour
# FLAC et OGG qui portent un en-tete et un index de conteneur.
FORMATS = {
    "mp3":  ("MP3", "MPEG_LAYER_III", "audio/mpeg", True),
    "flac": ("FLAC", "PCM_16", "audio/flac", False),
    "wav":  ("WAV", "PCM_16", "audio/wav", True),
    "pcm":  (None, None, "audio/pcm", True),
}
UNSUPPORTED_FORMATS = {
    "aac": "aac demande un encodeur absent de l'image",
    # Contrainte du codec, pas du service : libopus n'accepte que 8, 12, 16, 24
    # et 48 kHz, or le modele sort du 44,1 kHz. Le supporter imposerait un
    # reechantillonnage, donc une dependance de plus.
    "opus": "opus exige 8, 12, 16, 24 ou 48 kHz, le modele sort du 44,1 kHz",
}

# Les 10 voix OpenAI mappees sur les 10 presets Supertonic. Purement pratique :
# sans cela, un client OpenAI qui demande "alloy" recoit un 404. Un nom inconnu
# reste resolu normalement (preset ou voix importee), donc M1..F5 marchent aussi.
OPENAI_VOICE_ALIASES = {
    "alloy": "M1", "ash": "M2", "ballad": "M3", "echo": "M4", "verse": "M5",
    "coral": "F1", "sage": "F2", "shimmer": "F3", "marin": "F4", "cedar": "F5",
}

state = ServerState(model=os.getenv("TTS_MODEL", "supertonic-3"),
                    custom_styles_dir=VOICES_DIR)
app = create_app(state=state)
router = APIRouter(tags=["openai"])


class SpeechRequest(BaseModel):
    """Corps de POST /v1/audio/speech, aligne sur la specification OpenAI."""

    input: str = Field(..., min_length=1, description="texte a synthetiser")
    # `model` est requis par OpenAI mais ignore ici : un seul modele est charge.
    # Le refuser casserait tout client envoyant "tts-1" ou "gpt-4o-mini-tts".
    model: Optional[str] = Field(None, description="ignore, un seul modele est charge")
    voice: str = Field(STREAM_VOICE, description="voix OpenAI, preset M1..F5, ou voix importee")
    response_format: str = Field("mp3", description="mp3, opus, flac, wav ou pcm")
    # OpenAI autorise 0.25 a 4.0, le modele 0.7 a 2.0 : on borne au lieu de
    # rejeter, sinon un client OpenAI legitime se prend un 400.
    speed: Optional[float] = Field(None, ge=0.25, le=4.0)
    stream_format: Optional[str] = Field(None, description="audio ou sse")
    instructions: Optional[str] = Field(None, description="accepte et ignore")
    # Extensions Supertonic, qu'un client OpenAI n'enverra jamais.
    lang: Optional[str] = None
    steps: Optional[int] = Field(None, ge=1, le=100)
    # 10 est le minimum impose par chunk_text : en deca il leve une ValueError.
    max_chunk_length: Optional[int] = Field(None, ge=10, le=10000)
    silence_duration: Optional[float] = Field(None, ge=0.0, le=10.0)


def pcm16(wav: np.ndarray) -> bytes:
    """float32 [-1,1] du modele -> PCM 16 bits little-endian."""
    return (np.clip(wav.reshape(-1), -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_header(sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """En-tete RIFF a taille indeterminee, pour un wav envoye au fil de l'eau."""
    unknown = 0xFFFFFFFF
    return (b"RIFF" + struct.pack("<I", unknown) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                          sample_rate * channels * bits // 8,
                          channels * bits // 8, bits)
            + b"data" + struct.pack("<I", unknown))


def encode(wav: np.ndarray, fmt: str, sample_rate: int) -> bytes:
    """Encode un tableau audio complet dans le format demande."""
    if fmt == "pcm":
        return pcm16(wav)
    sf_format, subtype = FORMATS[fmt][0], FORMATS[fmt][1]
    buf = io.BytesIO()
    sf.write(buf, wav.reshape(-1), sample_rate, format=sf_format, subtype=subtype)
    return buf.getvalue()


def resolve_format(value: str) -> str:
    """Valide response_format, avec un message qui dit quoi utiliser a la place."""
    if value in UNSUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"{value} non supporte ({UNSUPPORTED_FORMATS[value]}). "
                   f"Formats disponibles : {', '.join(FORMATS)}.")
    if value not in FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"response_format inconnu : {value}. "
                   f"Formats disponibles : {', '.join(FORMATS)}.")
    return value


def prepare(req: SpeechRequest) -> dict:
    """Resout voix et parametres AVANT que la reponse ne commence.

    Indispensable pour le streaming : dans un generateur, ces erreurs ne
    surviendraient qu'a la premiere iteration, donc une fois les en-tetes deja
    envoyes. Le client recevrait un 200 tronque au lieu d'un 400.
    """
    try:
        style = _resolve_voice(state, OPENAI_VOICE_ALIASES.get(req.voice, req.voice))
    except UnknownVoice as exc:
        # Meme code que les routes du SDK sur la meme erreur.
        raise HTTPException(status_code=400, detail=f"voix inconnue : {exc}")
    chunks = chunk_text(req.input, req.max_chunk_length or STREAM_MAX_CHUNK)
    if not chunks:
        # chunk_text rend une liste vide sur du blanc (" ", "\n"), et
        # np.concatenate([]) leverait alors une erreur en plein traitement.
        raise HTTPException(status_code=400, detail="input ne contient aucun texte")
    silence_dur = (req.silence_duration if req.silence_duration is not None
                   else STREAM_SILENCE)
    return {
        "style": style,
        # OpenAI autorise 0.25 a 4.0, le modele 0.7 a 2.0 : on borne.
        "speed": min(max(req.speed if req.speed is not None else STREAM_SPEED,
                         MIN_SPEED), MAX_SPEED),
        "steps": req.steps if req.steps is not None else STREAM_STEPS,
        "lang": req.lang if req.lang is not None else STREAM_LANG,
        "chunks": chunks,
        "silence": np.zeros(int(silence_dur * state.tts.sample_rate), dtype=np.float32),
    }


def synthesize_segments(p: dict):
    """Genere les segments audio l'un apres l'autre, dans l'ordre du texte."""
    tts = state.tts
    chunks, silence = p["chunks"], p["silence"]
    for i, chunk in enumerate(chunks):
        # Meme verrou que les routes du SDK : ONNX Runtime n'est pas reentrant
        # et FastAPI execute les handlers sync dans un threadpool.
        with state.synth_lock:
            wav, _ = tts.synthesize(chunk, voice_style=p["style"], lang=p["lang"],
                                    total_steps=p["steps"], speed=p["speed"])
        audio = wav.reshape(-1)
        if i < len(chunks) - 1 and silence.size:
            audio = np.concatenate([audio, silence])
        yield audio


@router.post("/v1/audio/speech",
             responses={200: {"content": {"audio/mpeg": {}, "audio/wav": {},
                                          "audio/ogg": {}, "audio/flac": {},
                                          "audio/pcm": {}, "text/event-stream": {}},
                              "description": "audio synthetise"}})
def openai_speech(req: SpeechRequest):
    """Synthese compatible OpenAI, avec ou sans streaming."""
    fmt = resolve_format(req.response_format.lower().strip())
    _, _, mime, streamable = FORMATS[fmt]
    sr = state.tts.sample_rate

    mode = None
    if req.stream_format is not None:
        mode = req.stream_format.lower().strip()
        if mode not in ("audio", "sse"):
            raise HTTPException(
                status_code=400,
                detail=f"stream_format inconnu : {req.stream_format}. "
                       f"Valeurs acceptees : audio, sse.")

    # Tout ce qui peut echouer est resolu ici, avant le moindre octet de reponse.
    p = prepare(req)

    if mode is None:
        audio = np.concatenate(list(synthesize_segments(p)))
        return Response(
            content=encode(audio, fmt, sr), media_type=mime,
            headers={"Content-Disposition": f'inline; filename="speech.{fmt}"'})

    def blocks():
        """Emet les morceaux encodes, au fil de la synthese quand c'est possible."""
        if not streamable:
            # FLAC porte un en-tete et un index : des morceaux encodes separement
            # ne se concatenent pas. Le client recoit donc un fichier complet, en
            # un seul bloc.
            yield encode(np.concatenate(list(synthesize_segments(p))), fmt, sr)
            return
        if fmt == "wav":
            yield wav_header(sr)
        for audio in synthesize_segments(p):
            yield pcm16(audio) if fmt in ("wav", "pcm") else encode(audio, fmt, sr)

    if mode == "audio":
        return StreamingResponse(
            blocks(), media_type=mime,
            headers={"Content-Disposition": f'inline; filename="speech.{fmt}"',
                     "Cache-Control": "no-store"})

    def sse():
        """Evenements speech.audio.delta puis speech.audio.done, audio en base64."""
        for block in blocks():
            payload = {"type": "speech.audio.delta",
                       "audio": base64.b64encode(block).decode("ascii")}
            yield f"data: {json.dumps(payload)}\n\n"
        # Le service ne compte pas de tokens : l'estimation usuelle de 4
        # caracteres par token evite de renvoyer un objet vide aux clients qui
        # lisent ce champ.
        approx = max(1, len(req.input) // 4)
        yield "data: " + json.dumps(
            {"type": "speech.audio.done",
             "usage": {"input_tokens": approx, "output_tokens": 0,
                       "total_tokens": approx}}) + "\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


app.include_router(router)

# create_app a deja enregistre la route /v1/audio/speech du SDK. On remonte la
# notre devant, sinon elle ne serait jamais atteinte : Starlette retient la
# premiere qui correspond. On retire ensuite celle du SDK du routeur d'origine,
# faute de quoi c'est elle que /docs decrirait, et la doc mentirait sur les
# formats et sur stream_format.
app.routes.insert(0, app.routes.pop())
for _route in app.routes:
    _original = getattr(_route, "original_router", None)
    # `is not router` est essentiel : sans ce garde-fou on retirerait aussi la
    # notre, et plus aucune definition n'apparaitrait dans /docs.
    if _original is not None and _original is not router:
        _original.routes = [r for r in _original.routes
                            if getattr(r, "path", None) != "/v1/audio/speech"]
