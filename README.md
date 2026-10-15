# Employee Attendance & Analytics API

FastAPI + PyMongo service for employee attendance, regularization and analytics.

## Run locally (Windows PowerShell)

1. Install Python 3.11 or newer.
2. Create a free MongoDB Atlas cluster, create a database user, add your current IP to Network Access, and copy the Python connection string. Replace the password placeholder in the string; do not commit it.
3. From this folder run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI="mongodb+srv://USERNAME:PASSWORD@YOUR-CLUSTER.mongodb.net/?retryWrites=true&w=majority"
$env:MONGO_DB="attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

4. Open `http://127.0.0.1:8000/docs`. `/health` checks the MongoDB connection. Indexes are created automatically on startup.

On macOS/Linux, activate the virtual environment with `source .venv/bin/activate` and export `MONGO_URI` and `MONGO_DB` before running the commands.

## Notes

- MongoDB 6.0+ is required; MongoDB 7.0 is recommended.
- Keep credentials in environment variables or a local `.env` file, never in Git.
- `sample_seed.py` loads the provided sample data. Run it only against a development database because it inserts sample records.
- Analytics are implemented with MongoDB aggregation pipelines. Test edge cases beyond the supplied sample before submission; the provided sample alone is not a complete verification.
- Submit `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README in a public Git repository. Do not commit `.env`, secrets, a virtual environment, a Dockerfile, or data dumps.
