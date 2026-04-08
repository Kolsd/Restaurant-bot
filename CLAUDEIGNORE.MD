# === Bloqueo de archivos pesados e irrelevantes ===
__pycache__/
*.pyc
.env
.git/
.pytest_cache/
venv/
.venv/

# === Multimedia y Estáticos (Consumen mucho contexto) ===
app/static/img/*
app/static/js/vendor/*

# === Historial de Base de Datos ===
# Solo actívalo si no estás trabajando en migraciones
alembic/versions/*.py

# === Tests (Opcional) ===
# Descomenta si solo vas a desarrollar lógica y no quieres que lea los tests
# tests/