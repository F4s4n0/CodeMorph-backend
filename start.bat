@echo off
title Avvio Backend FastAPI - MigraCadabra
echo Avvio del server Uvicorn in modalita --reload...
uvicorn main:app --reload

pause