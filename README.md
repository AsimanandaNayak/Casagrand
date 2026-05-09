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

## Deploy on Render

1. Push this repo to GitHub (already done if `origin` is set).
2. In [Render](https://dashboard.render.com): **New** → **Blueprint** → connect `AsimanandaNayak/Casagrand` (or **Web Service** and point at the repo).
3. If you use **Web Service** manually instead of Blueprint:
   - **Runtime:** Python  
   - **Build command:** `pip install -r requirements.txt`  
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`  
4. Deploy. Open **`https://YOUR-SERVICE.onrender.com/docs`** (interactive API).  
   `GET /` returns `{"status":"ok","docs":"/docs"}` for health checks.

The app reads `data.json` from the filesystem at startup; ensure that file stays in the repo or mount it if you move data elsewhere.

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

