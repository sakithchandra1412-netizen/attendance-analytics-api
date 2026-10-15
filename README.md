# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee attendance tracking, regularization, and department analytics.

## Tech Stack
- **FastAPI** (Python 3.11+)
- **MongoDB 7.0** (PyMongo with timezone awareness)
- **Uvicorn**

## Setup & Running

1. **Install dependencies:**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Set `MONGO_URI` and `MONGO_DB` (defaults to `mongodb://localhost:27017` and `attendance_db` if not set):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Run the server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All required database indexes are created idempotently at startup.

## Project Structure
- `SOLUTION_ARCHITECTURE.md`: Comprehensive solution architecture, system diagrams, data models, aggregation pipeline breakdowns, and concurrency designs.
- `app/main.py`: Complete FastAPI application containing all endpoints, business rules (R1 - R10), and MongoDB aggregation pipelines.
- `app/dashboard.html`: Interactive web dashboard for employee switching, punch actions, logs, and analytics visualization.
- `requirements.txt`: Python dependencies required by `app/main.py`.
- `REVIEW.md`: Comprehensive review and defect log for starter code.
- `DECISIONS.md`: Design rationale covering indexes, race condition handling, tie-breaking, headcount aggregation, and scalability.
- `DATA_MODEL.md` & `openapi.yaml`: Data model specification and API contract.
