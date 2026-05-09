# Location Data API (FastAPI)

This project exposes an API over `data.json` so you can request **all rows for a given `Location`**.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open docs at `http://127.0.0.1:8000/docs`.

## Endpoint

- `GET /locations/{location}` (returns all rows for that location)
  - Automatically applies **fuzzy matching** for small spelling mistakes.
  - Optional BHK filter: `?bhk=...` (also fuzzy)

### Examples

- `GET /locations/Kilpauk%20,%20Chennai`
- Typo still works (fuzzy):
  - `GET /locations/kelambakam%20,%20chennnai`
- Filter by BHK (supports partial + fuzzy):
  - `GET /locations/Tambaram?bhk=3bhk`
  - `GET /locations/Tambaram?bhk=3%20bhk%20%2B%202t`

