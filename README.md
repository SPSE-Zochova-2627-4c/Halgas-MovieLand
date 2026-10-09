MovieLand — Návod na nastavenie

Čo potrebujete pred začiatkom

* **Python 3.10 alebo novší** – stiahnite si ho na https://www.python.org/downloads/
* **Bezplatný TMDB API kľúč** – získate ho na https://www.themoviedb.org/settings/api (vyžaduje bezplatný účet)

---

Krok 1 — Pridajte svoj API kľúč

Otvorte súbor **`api.env`** (nachádza sa v hlavnom priečinku projektu) a nahraďte tieto dve hodnoty:

```env
TMDB_API_KEY=your_tmdb_api_key_here
APP_SECRET=replace_with_a_long_random_secret
```

Vložte svoj TMDB API kľúč ako hodnotu `TMDB_API_KEY`.

Na vygenerovanie bezpečného `APP_SECRET` spustite v termináli:

```bash
python -c "import os; print(os.urandom(24).hex())"
```

Skopírujte vygenerovaný výstup a vložte ho ako hodnotu `APP_SECRET`.

---

Krok 2 — Spustenie aplikácie

Jednoducho dvojklikom spustite:

```text
run.bat
```

Alebo ho spustite z terminálu:

```bash
run.bat
```

Potom otvorte webový prehliadač a prejdite na:

```text
http://127.0.0.1:5000
```

---

Pravidlá registrácie

Registračná stránka vyžaduje:

* Používateľské meno: **3–20 znakov**
* Povolené sú iba: **písmená, čísla, `_`, `.`, `-`**
* **Medzery nie sú povolené**
* Heslo musí mať **minimálne 8 znakov**
* Pole na potvrdenie hesla sa musí zhodovať s heslom
* Používateľské mená obsahujúce zakázané slová budú odmietnuté
* Duplicitné používateľské mená nie sú povolené

---

Riešenie problémov

| Problém                              | Riešenie                                                    |
| ------------------------------------ | ----------------------------------------------------------- |
| `RuntimeError: TMDB_API_KEY not set` | Uistite sa, že ste vyplnili a uložili súbor `api.env`       |
| Aplikácia sa nespustí                | Skontrolujte, či máte nainštalovaný Python 3.10 alebo novší |
| Port je už používaný                 | Zmeňte port na konci súboru `main.py`: `app.run(port=5001)` |

---

O projekte

MovieLand je webová aplikácia na vyhľadávanie a objavovanie filmov využívajúca databázu **TMDB (The Movie Database)**. Používatelia si môžu vytvoriť účet, prezerať filmy a využívať ďalšie funkcie aplikácie po prihlásení.
