FROM python:3.12-slim

# UTF-8 partout (le modele gere 31 langues : accents, diacritiques) + cache sur /data
ENV PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    HOME=/data

WORKDIR /app

# SDK officiel Supertone (modele par defaut = supertonic-3, 31 langues) + hub
# + FastAPI/Uvicorn pour l'API HTTP (multipart = upload de voix)
RUN pip install --no-cache-dir "supertonic==1.3.1" huggingface_hub \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9"

COPY blend.py api.py /app/
# Utilisateur non privilegie : /data (volume) et /app/voices (bind) sont
# montes depuis l'hote, autant ne pas y ecrire en root.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /data /app/voices && chown -R app:app /data /app

USER app

# uvicorn ecoute 0.0.0.0 a l'interieur du conteneur : normal et necessaire.
# C'est docker-compose.yml qui decide de l'exposition reelle sur l'hote.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
