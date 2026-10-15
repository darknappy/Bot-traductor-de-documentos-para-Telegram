@echo off
rem Arranca el bot: crea el entorno virtual, instala dependencias y ejecuta.
cd /d "%~dp0"

if not exist .venv (
    echo Creando el entorno virtual...
    python -m venv .venv
    if errorlevel 1 (
        echo No se pudo crear el entorno virtual. Instala Python 3.10 o superior.
        pause
        exit /b 1
    )
)

echo Instalando dependencias...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo Fallo al instalar las dependencias.
    pause
    exit /b 1
)

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Se ha creado el archivo .env
    echo Abrelo con el Bloc de notas y pega tu token de @BotFather en TELEGRAM_BOT_TOKEN=
    echo.
    pause
    exit /b 1
)

echo Iniciando el bot... (Ctrl+C para detenerlo)
.venv\Scripts\python.exe bot.py
pause
