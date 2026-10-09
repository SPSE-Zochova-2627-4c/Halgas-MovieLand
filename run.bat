@echo off
echo ==========================================
echo  Spustanie Flask aplikacie (automaticky)
echo ==========================================
echo.

REM Vytvorenie virtualneho prostredia (ak neexistuje)
if not exist ".venv" (
    echo Vytvaram virtualne prostredie...
    py -3 -m venv .venv
)

echo Aktivujem virtualne prostredie...
call .venv\Scripts\activate

echo Aktualizujem pip...
python -m pip install --upgrade pip >nul

echo Kontrolujem balicky...
pip show flask >nul 2>&1 || pip install flask
pip show flask-sqlalchemy >nul 2>&1 || pip install flask-sqlalchemy
pip show requests >nul 2>&1 || pip install requests

echo.
echo Kontrola flasku...
where flask
echo Kontrola pythonu...
where python

echo.
echo ==========================================
echo  Spustam Flask aplikaciu...
echo ==========================================
echo.

flask --app main run --debug

pause