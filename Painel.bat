@echo off
title Painel de Cortes
cd /d "E:\CORTES"
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    start "" http://127.0.0.1:8765
) else (
    start "" http://127.0.0.1:8765
    python app.py
)
