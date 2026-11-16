@echo off
REM Teste curto (cerca de 2 minutos) para conferir se tudo funciona
cd /d "%~dp0"
docker compose up -d || goto erro
python -m pip install -r requirements.txt || goto erro
python executar_experimento.py --rapido || goto erro
echo.
echo Teste concluido. Veja a pasta resultados.
pause
exit /b 0
:erro
echo.
echo Algo deu errado. Copie a mensagem acima e mande para o Claude.
pause
exit /b 1
