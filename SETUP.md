# MovieLand — Návod na nastavenie

# 

# Čo potrebujete pred začiatkom

# 

# \- Python 3.10 alebo novší – stiahnite si ho na https://www.python.org/downloads/

# \- Bezplatný TMDB API kľúč – získate ho na https://www.themoviedb.org/settings/api (vyžaduje bezplatný účet)

# 

# \--------------------------------------------------

# 

# Krok 1 — Pridajte svoj API kľúč

# 

# Otvorte súbor "api.env" (nachádza sa v hlavnom priečinku projektu) a nahraďte tieto dve hodnoty:

# 

# TMDB\_API\_KEY=your\_tmdb\_api\_key\_here

# APP\_SECRET=replace\_with\_a\_long\_random\_secret

# 

# Vložte svoj TMDB API kľúč ako hodnotu TMDB\_API\_KEY.

# 

# Na vygenerovanie bezpečného APP\_SECRET spustite v termináli:

# 

# python -c "import os; print(os.urandom(24).hex())"

# 

# Skopírujte vygenerovaný výstup a vložte ho ako hodnotu APP\_SECRET.

# 

# \--------------------------------------------------

# 

# Krok 2 — Spustenie aplikácie

# 

# Jednoducho dvojklikom spustite:

# 

# run.bat

# 

# Alebo ho spustite z terminálu:

# 

# run.bat

# 

# Potom otvorte webový prehliadač a prejdite na:

# 

# http://127.0.0.1:5000

# 

# \--------------------------------------------------

# 

# Pravidlá registrácie

# 

# \- Používateľské meno: 3–20 znakov

# \- Povolené sú iba písmená, čísla, \_, . a -

# \- Medzery nie sú povolené

# \- Heslo musí mať minimálne 8 znakov

# \- Pole na potvrdenie hesla sa musí zhodovať s heslom

# \- Používateľské mená obsahujúce zakázané slová budú odmietnuté

# \- Duplicitné používateľské mená nie sú povolené

# 

# \--------------------------------------------------

# 

# Riešenie problémov

# 

# Problém:

# RuntimeError: TMDB\_API\_KEY not set

# 

# Riešenie:

# Uistite sa, že ste vyplnili a uložili súbor api.env

# 

# \--------------------

# 

# Problém:

# Aplikácia sa nespustí

# 

# Riešenie:

# Skontrolujte, či máte nainštalovaný Python 3.10 alebo novší

# 

# \--------------------

# 

# Problém:

# Port je už používaný

# 

# Riešenie:

# Zmeňte port na konci súboru main.py:

# 

# app.run(port=5001)

# 

# \--------------------------------------------------

# 

# O projekte

# 

# MovieLand je webová aplikácia na vyhľadávanie a objavovanie filmov využívajúca databázu TMDB (The Movie Database). Používatelia si môžu vytvoriť účet, prezerať filmy a využívať ďalšie funkcie aplikácie po prihlásení.

