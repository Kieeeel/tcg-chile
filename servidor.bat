@echo off
rem ===================================================================
rem  Radar TCG Chile - arranca el servidor con doble clic.
rem
rem  Los mensajes de este archivo van sin tildes a proposito: la consola
rem  de Windows no siempre usa la misma pagina de codigos, y un acento
rem  mal codificado en el aviso de un error hace dudar de si el problema
rem  es el error o el propio aviso. Lo que imprime Python si lleva.
rem ===================================================================

rem Trabajar siempre en la carpeta de este archivo, se lance desde donde
rem se lance: con un acceso directo, el directorio actual es otro.
cd /d "%~dp0"

chcp 65001 >nul 2>&1
title Radar TCG Chile - servidor

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo  No encuentro el entorno virtual en:
    echo     %CD%\%PY%
    echo.
    echo  Crealo con:  python -m venv .venv
    echo  y luego:     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem El fallo mas probable, con diferencia: ya hay un servidor en el 8000.
rem Sin esta comprobacion, uvicorn arranca, dice "startup complete" y
rem revienta despues al reservar el puerto, que se lee como si el error
rem fuera del programa cuando en realidad solo sobra un proceso.
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  El puerto 8000 ya esta ocupado: hay otro servidor corriendo.
    echo.
    rem El comando de abajo va SIN tuberias a proposito. Con `^|` cmd lo
    rem imprimia con el acento circunflejo incluido, y copiarlo tal cual no
    rem funcionaba: un remedio que no cura es peor que no dar ninguno.
    echo  Si es una ventana tuya, usa esa. Si quedo colgada, cierrala con:
    echo     powershell -c "Stop-Process -Force -Id (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess"
    echo.
    pause
    exit /b 1
)

echo.
echo  Arrancando Radar TCG Chile...
echo  Cuando termine de cargar se abre solo en el navegador.
echo.
echo  Para pararlo: Ctrl+C aqui, o cierra esta ventana.
echo.

rem El navegador, en paralelo y con margen para que el servidor llegue a
rem escuchar. `start` no espera, asi que la linea de abajo arranca ya.
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8000"

"%PY%" run.py

rem Si se llega aqui es que el servidor se ha parado. Con Ctrl+C es lo
rem normal; con un error, la pausa deja leerlo antes de que la ventana
rem se cierre y se lleve el mensaje por delante.
echo.
echo  El servidor se ha detenido.
pause
