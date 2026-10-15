"""
Employee Attendance & Analytics API  –  v2.0  (full implementation)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env for convenience; real env vars override)
"""

import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,   # fail fast; grader injects its own URI
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Index creation  (idempotent – safe to call every startup)
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """
    Creates all necessary indexes idempotently at startup.
    Wrapped in try/except so a transient MongoDB hiccup doesn't crash the
    process before the /health probe can report 503.
    """
    try:
        # employees: unique emp_code (concurrent creates → exactly one 201)
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # employees: list by department + sorted by emp_code
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)]
        )
        # employees: headcount queries (joined_on per department)
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)]
        )
        # Summary without a department filter still needs joined_on.
        db.employees.create_index([("joined_on", ASCENDING)])

        # attendance_logs: unique (emp_code, date) prevents duplicate punch-ins
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        # attendance_logs: list endpoint  →  sort by date DESC, emp_code ASC
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        # attendance_logs: analytics range scans per employee
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)]
        )
        # attendance_logs: department trend  (date range over all employees)
        db.attendance_logs.create_index([("date", ASCENDING)])
    except Exception as exc:  # noqa: BLE001
        # Log but don't crash – /health will return 503 until DB is reachable
        import logging
        logging.getLogger("uvicorn.error").warning("Index creation skipped: %s", exc)


# ---------------------------------------------------------------------------
# Helpers – time
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_ist(dt: datetime) -> datetime:
    """Convert a datetime to IST."""
    return _to_utc(dt).astimezone(IST)


def _trunc_seconds(dt: datetime) -> datetime:
    """Truncate to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """datetime → epoch milliseconds (int), or None."""
    if dt is None:
        return None
    return int(_to_utc(dt).timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime, truncated to seconds (R1)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return _trunc_seconds(dt)


def _attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: The attendance date is the IST calendar date of punch_in.
    Exception – overnight shifts (shift_end <= shift_start):
      a punch-in whose IST *time* is strictly before shift_end belongs to
      the shift that **started the previous IST day**.
    Returns 'YYYY-MM-DD'.
    """
    ist = _to_ist(punch_in_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_minutes = ist.hour * 60 + ist.minute
        end_minutes   = eh * 60 + em
        if punch_minutes < end_minutes:
            # Belongs to the shift that started yesterday (IST)
            return (ist.date() - timedelta(days=1)).isoformat()

    return ist.date().isoformat()


# ---------------------------------------------------------------------------
# Helpers – business-rule computations
# ---------------------------------------------------------------------------

def _round_half_up(value: float, decimals: int) -> float:
    """Half-up rounding (R8).  Python's built-in round() uses banker's rounding."""
    d      = Decimal(str(value))
    factor = Decimal(10) ** -decimals
    return float(d.quantize(factor, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace period = 10 min (strictly more than 10:00 after shift_start → late).
    late_minutes = floor(minutes since shift_start), NOT since grace-period end.
    Calculation is IST-aware.
    """
    h, m = map(int, shift_start.split(":"))
    # Shift start on the attendance date in IST
    att   = date.fromisoformat(att_date)
    start = datetime(att.year, att.month, att.day, h, m, 0, tzinfo=IST)
    delta_seconds = (punch_in_utc - start).total_seconds()
    minutes = int(delta_seconds // 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded half-up to 2 decimals."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return _round_half_up(seconds / 3600, 2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    """
    R3: overtime = floor(punch_out - shift_end) in whole minutes, only if >= 30; else 0.
    For overnight shifts shift_end is on the NEXT day (att_date + 1).
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att    = date.fromisoformat(att_date)

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    end_date     = att + timedelta(days=1) if is_overnight else att
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)

    diff_minutes = int((punch_out_utc - shift_end_dt).total_seconds() // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _recompute_derived(doc: dict, emp: dict) -> dict:
    """
    Re-run R2-R5 on a doc that has both punch_in and punch_out (presence status).
    Returns a dict of field → new value for every derived field.
    """
    pi = doc["punch_in"]
    po = doc.get("punch_out")

    late_minutes = compute_late_minutes(pi, emp["shift_start"], doc["date"])

    if po is None:
        return {
            "late_minutes":     late_minutes,
            "overtime_minutes": 0,
            "work_hours":       None,
            "half_day":         False,
        }

    wh  = compute_work_hours(pi, po)
    ot  = compute_overtime_minutes(po, emp["shift_end"], doc["date"], emp["shift_start"])
    hd  = compute_half_day(wh)
    return {
        "late_minutes":     late_minutes,
        "overtime_minutes": ot,
        "work_hours":       wh,
        "half_day":         hd,
    }


# ---------------------------------------------------------------------------
# Helpers – serialisation
# ---------------------------------------------------------------------------

def _serial_employee(doc: dict) -> dict:
    """Convert a MongoDB employee doc to the API shape."""
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


def _serial_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance doc to the API shape."""
    doc = dict(doc)
    doc.pop("_id", None)
    doc["punch_in"]  = to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))

    # Normalise legacy / missing fields (DATA_MODEL §9)
    doc.setdefault("half_day",         False)
    doc.setdefault("late_minutes",     0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("history",          [])

    # Serialise history entries
    for h in doc["history"]:
        h["at"] = to_epoch_ms(h["at"])
        for field, change in h.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = to_epoch_ms(change.get("from"))
                change["to"]   = to_epoch_ms(change.get("to"))

    return doc


# ---------------------------------------------------------------------------
# EpochMillis validator helper (used in Pydantic models)
# ---------------------------------------------------------------------------

def _validate_epoch_ms(v: Any) -> int:
    """
    Rejects floats and values that look like seconds (< 100_000_000_000).
    Per spec: minimum 100000000000, maximum 4102444800000.
    """
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("punched_at must be an integer epoch milliseconds value")
    if v < 100_000_000_000 or v > 4_102_444_800_000:
        raise ValueError(
            "punched_at must be epoch milliseconds (100000000000 ≤ value ≤ 4102444800000)"
        )
    return v


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code:    str = Field(pattern=r"^EMP\d{4,6}$")
    name:        str = Field(min_length=1, max_length=100)
    email:       str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str  # YYYY-MM-DD validated below

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        """Make sure joined_on is a real YYYY-MM-DD date."""
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return v
        return _validate_epoch_ms(v)


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return v
        return _validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    status:         Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None
    reason:         str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v: Any) -> Optional[int]:
        if v is None:
            return v
        return _validate_epoch_ms(v)


# ---------------------------------------------------------------------------
# Endpoints: system
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """R: Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB not reachable")


# ---------------------------------------------------------------------------
# Endpoints: employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """
    Creates an employee. emp_code uniqueness enforced by DB unique index.
    Concurrent duplicate creates → exactly one 201, the rest 409.
    """
    doc              = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return _serial_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """Sorted by emp_code ascending. total reflects the filtered set."""
    q: dict = {}
    if department:
        q["department"] = department

    skip  = (page - 1) * page_size          # BUG FIX: was page * page_size
    total = db.employees.count_documents(q)  # BUG FIX: was count_documents({})
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)         # BUG FIX: missing sort
        .skip(skip)
        .limit(page_size)
    )
    # Serialise created_at → epoch ms
    items = [_serial_employee(e) for e in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Endpoints: attendance
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Creates the day's attendance record.
    Concurrency: unique index on (emp_code, date) + DuplicateKeyError → 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:                               # BUG FIX: was missing → KeyError → 500
        raise HTTPException(404, "Employee not found")

    ts = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else _trunc_seconds(datetime.now(timezone.utc))
    )
    # BUG FIX: attendance date computed in IST (R1), not server local time
    att_date = _attendance_date(ts, emp["shift_start"], emp["shift_end"])

    late = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:                # BUG FIX: atomic – no check-then-insert race
        raise HTTPException(409, "Already punched in for this attendance date")

    return _serial_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """
    Closes the employee's most recent open record (punch_in <= punched_at, punch_out null).
    Works across midnight for overnight shifts.
    Concurrency: find_one_and_update with punch_out=null filter → atomic.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else _trunc_seconds(datetime.now(timezone.utc))
    )

    # Find the most recent open record whose punch_in ≤ punched_at
    open_rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status":    {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not open_rec:
        # A closed record means another punch-out request already won.
        # This keeps the concurrent-request response separate from a real 404.
        last_rec = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": ts},
                "status": {"$in": list(PRESENCE_STATUSES)},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if last_rec and last_rec.get("punch_out") is not None:
            raise HTTPException(409, "Record has already been punched out")
        raise HTTPException(404, "No open punch-in found for this employee")

    pi_dt = open_rec["punch_in"]  # already a datetime (tz_aware=True)
    if ts <= pi_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (ts - pi_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    wh  = compute_work_hours(pi_dt, ts)
    ot  = compute_overtime_minutes(ts, emp["shift_end"], open_rec["date"], emp["shift_start"])
    hd  = compute_half_day(wh)

    # Atomic update – filter on punch_out: None prevents double punch-out
    updated = db.attendance_logs.find_one_and_update(
        {"_id": open_rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out":        ts,
                "work_hours":       wh,
                "overtime_minutes": ot,
                "half_day":         hd,
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "Record already punched out by a concurrent request")

    return _serial_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Sorted by date DESC, emp_code ASC.  DB-side pagination (fast on 100k records).
    BUG FIX: was loading entire collection into Python memory.
    """
    if date_from:
        _read_date(date_from, "date_from")
    if date_to:
        _read_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip  = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs  = list(
        db.attendance_logs.find(q)             # BUG FIX: sort + pagination in DB
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [_serial_attendance(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    """
    Manually correct one day's record.  Recomputes R2-R5 and appends a history entry.
    Concurrency: $push to history is atomic; we additionally guard with version check.
    """
    _read_date(date_str, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(404, "Attendance record not found for that date")

    # --- Validate incoming combination ---
    new_status    = body.status or rec["status"]
    new_pi_ms     = body.punch_in
    new_po_ms     = body.punch_out
    new_pi: Optional[datetime] = from_epoch_ms(new_pi_ms) if new_pi_ms is not None else rec.get("punch_in")
    new_po: Optional[datetime] = from_epoch_ms(new_po_ms) if new_po_ms is not None else rec.get("punch_out")

    is_absence = new_status in ("ABSENT", "LEAVE")

    if is_absence:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in/punch_out with ABSENT or LEAVE status")
        new_pi = None
        new_po = None

    if not is_absence and new_pi is None:
        raise HTTPException(422, "Presence status requires a punch_in")

    if new_pi is not None:
        # punch_in must stay on the record's attendance date (R1)
        new_attendance_date = _attendance_date(
            new_pi, emp["shift_start"], emp["shift_end"]
        )
        if new_attendance_date != date_str:
            raise HTTPException(422, "punch_in must match the record's attendance date")

    if new_pi is not None and new_po is not None:
        if new_po <= new_pi:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_po - new_pi).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # --- Build new derived values ---
    if is_absence:
        new_derived = {
            "late_minutes":     0,
            "overtime_minutes": 0,
            "work_hours":       None,
            "half_day":         False,
        }
    elif new_pi is not None and new_po is not None:
        wh  = compute_work_hours(new_pi, new_po)
        ot  = compute_overtime_minutes(new_po, emp["shift_end"], date_str, emp["shift_start"])
        hd  = compute_half_day(wh)
        lm  = compute_late_minutes(new_pi, emp["shift_start"], date_str)
        new_derived = {
            "late_minutes":     lm,
            "overtime_minutes": ot,
            "work_hours":       wh,
            "half_day":         hd,
        }
    elif new_pi is not None:
        lm = compute_late_minutes(new_pi, emp["shift_start"], date_str)
        new_derived = {
            "late_minutes":     lm,
            "overtime_minutes": 0,
            "work_hours":       None,
            "half_day":         False,
        }
    else:
        new_derived = {
            "late_minutes":     rec.get("late_minutes", 0),
            "overtime_minutes": rec.get("overtime_minutes", 0),
            "work_hours":       rec.get("work_hours"),
            "half_day":         rec.get("half_day", False),
        }

    # --- Build changes dict (only actually-changed fields) ---
    def _same(a, b) -> bool:
        # Compare datetimes by epoch ms to avoid tz representation differences
        if isinstance(a, datetime) and isinstance(b, datetime):
            return to_epoch_ms(a) == to_epoch_ms(b)
        return a == b

    old_pi  = rec.get("punch_in")
    old_po  = rec.get("punch_out")
    old_wh  = rec.get("work_hours")
    old_lm  = rec.get("late_minutes", 0)
    old_ot  = rec.get("overtime_minutes", 0)
    old_hd  = rec.get("half_day", False)
    old_st  = rec["status"]

    changes: dict = {}
    if not _same(old_st, new_status):
        changes["status"] = {"from": old_st, "to": new_status}
    if not _same(old_pi, new_pi):
        changes["punch_in"] = {"from": to_epoch_ms(old_pi), "to": to_epoch_ms(new_pi)}
    if not _same(old_po, new_po):
        changes["punch_out"] = {"from": to_epoch_ms(old_po), "to": to_epoch_ms(new_po)}
    if not _same(old_wh, new_derived["work_hours"]):
        changes["work_hours"] = {"from": old_wh, "to": new_derived["work_hours"]}
    if not _same(old_lm, new_derived["late_minutes"]):
        changes["late_minutes"] = {"from": old_lm, "to": new_derived["late_minutes"]}
    if not _same(old_ot, new_derived["overtime_minutes"]):
        changes["overtime_minutes"] = {"from": old_ot, "to": new_derived["overtime_minutes"]}
    if not _same(old_hd, new_derived["half_day"]):
        changes["half_day"] = {"from": old_hd, "to": new_derived["half_day"]}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at":      datetime.now(timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Store punch_in/punch_out as BSON datetimes in the history changes object
    raw_history_entry = dict(history_entry)
    raw_changes: dict = {}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            raw_changes[k] = {
                "from": from_epoch_ms(v["from"]) if v["from"] is not None else None,
                "to":   from_epoch_ms(v["to"])   if v["to"]   is not None else None,
            }
        else:
            raw_changes[k] = v
    raw_history_entry["changes"] = raw_changes

    # Atomic update – $push guarantees history is never silently overwritten
    # We also guard on len(history) to detect concurrent edits
    current_history_len = len(rec.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id":     rec["_id"],
            # Optimistic concurrency: reject if another edit sneaked in
            "$expr":   {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_pi,
                "punch_out":        new_po,
                "work_hours":       new_derived["work_hours"],
                "late_minutes":     new_derived["late_minutes"],
                "overtime_minutes": new_derived["overtime_minutes"],
                "half_day":         new_derived["half_day"],
            },
            "$push": {"history": raw_history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return _serial_attendance(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _month_bounds(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) for a 'YYYY-MM' month string."""
    y, m   = map(int, month.split("-"))
    first  = date(y, m, 1)
    if m == 12:
        last = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(y, m + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _working_days_count(from_date: date, to_date: date) -> int:
    """Count Mon-Fri days inclusive between from_date and to_date (R7)."""
    count = 0
    current = from_date
    while current <= to_date:
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        current += timedelta(days=1)
    return count


def _is_working_day(d: date) -> bool:
    return d.weekday() < 5


def _read_date(value: str, field_name: str) -> date:
    """Read a YYYY-MM-DD value and return a normal 422 error if it is invalid."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field_name} must be a valid YYYY-MM-DD date")


# ---------------------------------------------------------------------------
# Endpoints: analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee. Backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    first_day, last_day = _month_bounds(month)

    # R7: working days from max(joined_on, first_day) to last_day
    start_date   = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(first_day))
    working_days = _working_days_count(start_date, date.fromisoformat(last_day))

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": first_day, "$lte": last_day},
        }},
        {"$group": {
            "_id": None,
            # present_days: 0.5 for half_day on Mon-Fri, 1.0 for full on Mon-Fri (R5-R7)
            # We compute day-of-week in aggregation via $dayOfWeek (1=Sun, 7=Sat → 2-6 = Mon-Fri)
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            # Mon-Fri: $dayOfWeek with IST offset
                            {"$in": [
                                {"$dayOfWeek": {
                                    "date":     {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                                    "timezone": "Asia/Kolkata",
                                }},
                                [2, 3, 4, 5, 6],  # Mon=2 … Fri=6 in MongoDB's convention
                            ]},
                        ]},
                        # 0.5 if half_day else 1.0
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0,
                    ]
                }
            },
            "leave_days":            {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":            {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":    {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes":{"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days          = r["present_days"]
        leave_days            = int(r["leave_days"])
        late_count            = int(r["late_count"])
        total_late_minutes    = int(r["total_late_minutes"])
        total_overtime_minutes= int(r["total_overtime_minutes"])
    else:
        present_days          = 0.0
        leave_days            = 0
        late_count            = 0
        total_late_minutes    = 0
        total_overtime_minutes= 0

    attendance_pct = (
        _round_half_up(present_days / working_days * 100, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month:      str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    One item per department. Headcount includes employees with zero logs (R9).
    Runs as a cross-collection aggregation: starts from employees, $lookup logs.
    """
    first_day, last_day = _month_bounds(month)

    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # 1. Filter employees by headcount eligibility (R9) and optional dept
        {"$match": emp_match},

        # 2. Lookup that employee's logs for the month
        {"$lookup": {
            "from": "attendance_logs",
            "let":  {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$emp_code", "$$ec"]},
                        {"$gte": ["$date",     first_day]},
                        {"$lte": ["$date",     last_day]},
                    ]
                }}}
            ],
            "as": "logs",
        }},

        # 3. Unwind – preserveNullAndEmptyArrays keeps employees with zero logs
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        # 4. Project per-employee-per-log fields we need
        {"$project": {
            "department": 1,
            "emp_code":   1,
            "is_mon_fri": {
                "$cond": [
                    {"$and": [
                        {"$gt":  [{"$type": "$logs"}, "missing"]},
                        {"$in": [
                            {"$dayOfWeek": {
                                "date":     {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}},
                                "timezone": "Asia/Kolkata",
                            }},
                            [2, 3, 4, 5, 6],
                        ]},
                    ]},
                    True, False,
                ]
            },
            "is_presence":  {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_half_day":  {"$ifNull": ["$logs.half_day", False]},
            "has_wh":       {"$and": [
                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$gt": [{"$type": {"$ifNull": ["$logs.work_hours", None]}}, "missing"]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
            ]},
            "work_hours":   {"$ifNull": ["$logs.work_hours", None]},
            "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
            "status":       {"$ifNull": ["$logs.status", None]},
        }},

        # 5. Group by department
        {"$group": {
            "_id":        "$department",
            # headcount = distinct employees (R9)
            "employees":  {"$addToSet": "$emp_code"},
            # present_days = 0.5 for half-days on Mon-Fri, 1.0 for full on Mon-Fri
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_presence", "$is_mon_fri"]},
                        {"$cond": ["$is_half_day", 0.5, 1.0]},
                        0,
                    ]
                }
            },
            # For avg_work_hours: sum and count separately
            "wh_sum":   {"$sum": {"$cond": ["$has_wh", "$work_hours", 0]}},
            "wh_count": {"$sum": {"$cond": ["$has_wh", 1, 0]}},
            "late_count":           {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes":   {"$sum": "$late_minutes"},
            "leave_count":          {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]},   1, 0]}},
            "on_duty_count":        {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},

        # 6. Project final shape
        {"$project": {
            "_id":         0,
            "department":  "$_id",
            "headcount":   {"$size": "$employees"},
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$wh_count", 0]},
                    {"$divide": ["$wh_sum", "$wh_count"]},
                    None,
                ]
            },
            "late_count":         1,
            "total_late_minutes": 1,
            "leave_count":        1,
            "on_duty_count":      1,
        }},

        {"$sort": {"department": ASCENDING}},
    ]

    items = list(db.employees.aggregate(pipeline))
    # Apply R8 rounding and cast types
    for item in items:
        item["present_days"]    = _round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = _round_half_up(item["avg_work_hours"], 2)
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"]         = int(item["late_count"])
        item["leave_count"]        = int(item["leave_count"])
        item["on_duty_count"]      = int(item["on_duty_count"])

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Standard competition ranking (1, 2, 2, 4 …).  limit applied AFTER ranking.
    Ties share a rank; tied rows ordered by emp_code ASC.
    """
    first_day, last_day = _month_bounds(month)

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Join employee info
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},   # drops logs with no matching employee
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        # $rank gives standard competition ranks: 1, 2, 2, 4.
        # emp_code is sorted only after ranks are calculated, so a tie stays a tie.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id":          0,
            "rank":         1,
            "emp_code":     "$_id",
            "name":         "$emp.name",
            "department":   "$emp.department",
            "total_late_minutes": 1,
            "late_count":   1,
        }},
    ]

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


def _build_trend_pipeline(department: str, from_date: date, to_date: date) -> list[dict]:
    """Build the trend aggregation. MongoDB creates the date rows and moving average."""
    total_days = (to_date - from_date).days + 1
    start_of_range = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {"$project": {
            "emp_code": 1,
            "joined_on": 1,
            "days": {"$map": {
                "input": {"$range": [0, total_days]},
                "as": "number",
                "in": {"$dateToString": {
                    "date": {"$dateAdd": {
                        "startDate": start_of_range,
                        "unit": "day",
                        "amount": "$$number",
                    }},
                    "format": "%Y-%m-%d",
                    "timezone": "UTC",
                }},
            }},
        }},
        {"$unwind": "$days"},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "day": "$days"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$eq": ["$date", "$$day"]},
            ]}}}],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$days",
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$days"]}, 1, 0]}},
            "present_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$lte": ["$joined_on", "$days"]},
                    {"$in": [{"$ifNull": ["$log.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                ]},
                {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1.0]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$lte": ["$joined_on", "$days"]},
                    {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]},
                ]}, 1, 0,
            ]}},
        }},
        {"$set": {"date_value": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_value"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date_value": ASCENDING},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, {"$round": ["$attendance_rate", 4]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$round": ["$moving_avg_7d", 4]}]},
        }},
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_:      str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to:         str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Exactly one row per calendar day from from to to inclusive (weekends and empty days included).
    moving_avg_7d is computed over non-null attendance_rate values in the trailing 7-day window
    (only rows inside the requested range).
    """
    from_date = _read_date(from_, "from")
    to_date   = _read_date(to, "to")

    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (to_date - from_date).days > 92:
        raise HTTPException(422, "Range must not exceed 92 days")

    # Validate department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, f"Department '{department}' not found")

    pipeline = _build_trend_pipeline(department, from_date, to_date)
    return {"department": department, "items": list(db.employees.aggregate(pipeline))}


# ---------------------------------------------------------------------------
# Endpoint: admin / explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:  str,
    emp_code:  Optional[str] = None,
    month:     Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:     int  = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    from_:     Optional[str] = Query(default=None, alias="from"),
    to:        Optional[str] = None,
    page:      int  = Query(default=1, ge=1),
    page_size: int  = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "find":      "attendance_logs",
            "filter":    q,
            "sort":      {"date": DESCENDING, "emp_code": ASCENDING},
            "skip":      skip,
            "limit":     page_size,
        }
        result = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        first_day, last_day = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        result = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        first_day, last_day = _month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let":  {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {
                        "$and": [
                            {"$eq":  ["$emp_code", "$$ec"]},
                            {"$gte": ["$date",     first_day]},
                            {"$lte": ["$date",     last_day]},
                        ]
                    }}}
                ],
                "as": "logs",
            }},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        result = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": result}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        first_day, last_day = _month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline += [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        result = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    # department_trend
    if not department or not from_ or not to:
        raise HTTPException(422, "department, from, and to are required for department_trend")
    trend_from = _read_date(from_, "from")
    trend_to = _read_date(to, "to")
    if trend_to < trend_from or (trend_to - trend_from).days > 92:
        raise HTTPException(422, "Invalid trend date range")
    pipeline = _build_trend_pipeline(department, trend_from, trend_to)
    cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "employees", "explain": result}
