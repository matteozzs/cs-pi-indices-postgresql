@echo off
REM Experimento completo (cerca de 1 a 2 horas). Feche outros programas antes.
cd /d "%~dp0"
docker compose up -d || goto erro
python -m pip install -r requirements.txt || goto erro
python executar_experimento.py || goto erro
echo.
echo Experimento concluido. Compacte a pasta mais recente dentro de resultados.
pause
exit /b 0
:erro
echo.
echo Algo deu errado. Copie a mensagem acima e mande para o Claude.
pause
exit /b 1
