# Employee Attendance and Analytics API

This project is a FastAPI and MongoDB backend for managing employee attendance. Employees can be created, punched in and out, and their attendance can be corrected by an administrator. The API also provides monthly, department, leaderboard, and trend analytics.

All application code is in `app/main.py`.

## Features

- Employee creation and paginated employee listing
- Attendance punch-in and punch-out
- Attendance regularization with an audit history
- IST and overnight-shift handling
- Late minutes, overtime, work-hours, and half-day calculations
- Employee monthly analytics
- Department summary, late leaderboard, and department trend analytics
- MongoDB indexes created automatically at startup
- MongoDB explain endpoint for checking query plans

## Requirements

- Python 3.11 or later
- MongoDB 6.0 or later

## Setup and run

Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Seed the sample data and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

The readiness endpoint is available at `GET /health`. API documentation is available at `http://127.0.0.1:8000/docs` while the server is running.

## Project files

- `app/main.py` - FastAPI application and MongoDB queries
- `openapi.yaml` - API contract and business rules
- `DATA_MODEL.md` - MongoDB collection structure
- `REVIEW.md` - Review findings from the starter code
- `DECISIONS.md` - Design and implementation decisions

## AI assistance disclosure

AI assistance was used during development to review requirements, identify implementation issues, and edit portions of the code and documentation. The resulting implementation was reviewed and validated locally by the author. The author remains responsible for understanding, testing, and submitting the final work.

## Submission note

Do not commit `.env`, virtual environments, sample database dumps, or other local files containing credentials or machine-specific data.


## User Review
I have used AI to help with the solution. Though this may cause my rejection I am being honest with my work.
ThankYou for considering my application. 
The test was very interesting and I had fun solving the problem.