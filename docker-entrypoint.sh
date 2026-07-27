#!/bin/sh
# Bring the schema up to date before the bot starts taking updates.
# Safe to run on every boot: Alembic is a no-op when already at head.
set -e

echo "Applying database migrations..."
alembic upgrade head

exec "$@"
