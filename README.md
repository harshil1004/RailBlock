# RailBlock AI

A presentation-ready maintenance-block planning prototype for the Bengaluru–Dharmavaram corridor.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000`.

Run the dependency-free rule tests with:

```bash
python3 -m unittest discover -s tests -v
```

Application data is stored in CSV files under `data/`. The included demonstration data is loaded directly from those files; no PostgreSQL server or `DATABASE_URL` is required.

The demo has TMS, SMMS, TDMS, and COA views. Departments submit KM-range requests and receive an explainable ML-style risk score. COA can monitor, complete, or extend a live block by 30/60 minutes. Passenger/express showcase data is timetable-derived; freight entries are explicitly COA forecasts.

Operational actions enforce valid request transitions, require rejection reasons, enforce the 24-hour coordination hold before COA review (except Critical work), record an audit trail, and reject overlapping same-day possessions. Use `/health` to verify CSV storage.

## API

- `GET /api/state` – requests, blocks, and trains for the operational UI
- `GET /api/insights` – asset, weather, traffic, and recommendation data
- `POST /api/requests` – submit a departmental maintenance request
- `POST /api/requests/{request_id}` – release, approve, or reject a request
- `GET /api/requests/{request_id}/plan` – find a conflict-free candidate window
- `POST /api/manual-blocks` – create a non-overlapping manual possession
- `POST /api/simulate` – assess a proposed window

Planning is deterministic and rule-based: it considers candidate windows, train movements, existing blocks, weather, and KM-range overlap. It is not a machine-learning or OR-Tools optimizer.

## Live railway traffic integration

Live traffic is accessed only by the backend through `RailTrafficProvider`; the API key is never sent to browser code. Copy `.env.example` to `.env`, set `RAIL_TRAFFIC_API_URL` and `RAIL_TRAFFIC_API_KEY`, then start Uvicorn with `uvicorn backend.main:app --reload --env-file .env`. Never commit `.env` or a real secret.

The backend exposes normalized traffic data through:

- `GET /api/traffic/live`
- `GET /api/traffic/trains/{train_id}`
- `GET /api/traffic/corridor?corridor=Bengaluru%20%E2%80%93%20Dharmavaram`

The provider adapter applies an 8-second timeout by default, retries transient failures and rate limits twice, reports empty feeds, and marks responses stale when their provider timestamp is older than `RAIL_TRAFFIC_STALE_AFTER_SECONDS`. Provider-specific payload fields are not returned to the frontend.
