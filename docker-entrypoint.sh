#!/bin/sh
set -e
# Cria/atualiza o banco (idempotente) e sobe o gunicorn.
python app.py init-db
exec gunicorn -c gunicorn.conf.py app:app
