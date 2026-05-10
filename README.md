# DoorFlow Production

Aplicatie web pentru evidenta comenzilor, sectii, angajati, predare cu accept/refuz, atasamente si monitorizare intarzieri.

## Ce include
- Login admin / angajat
- Sectii fixe pe flux de productie
- Predare catre sectie + angajat
- Inbox de receptie cu accept/refuz
- Refuz cu motiv si istoric
- Atasamente PDF / JPG / PNG / WEBP
- Setari admin pentru termene pe sectie
- Inregistrare automata a intarzierilor in istoric
- Pregatita pentru lansare online pe Render

## Conturi initiale
- Admin: `admin` / `admin123`
- Angajati demo: parola `1234`

Exemple useri:
- `tocuri1`
- `usicrude1`
- `vopsea1`
- `asamblare1`
- `control1`

Schimba parolele dupa prima pornire.

## Pornire locala
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Aplicatia va fi disponibila la:
```bash
http://127.0.0.1:5000
```

## Lansare online pe Render
### Varianta simpla
1. Creezi cont pe Render.
2. Urca proiectul pe GitHub.
3. In Render alegi **New > Blueprint** sau **New Web Service**.
4. Conectezi repository-ul.
5. Daca folosesti Blueprint, Render citeste automat `render.yaml`.
6. Daca folosesti Web Service manual:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
7. Adaugi o baza PostgreSQL in Render si setezi `DATABASE_URL`.
8. Setezi `SECRET_KEY`.

## Observatii
- In productie foloseste PostgreSQL, nu SQLite.
- Pentru fisiere mari sau multe atasamente, recomandat ulterior storage extern.
- Pentru prima versiune, intarzierile sunt calculate in zile calendaristice.
