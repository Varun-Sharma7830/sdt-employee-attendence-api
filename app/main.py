"""Employee Attendance & Analytics API. Run with: uvicorn app.main:app --port 8000"""
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
INDEXES_READY = False


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_punchin")


@app.on_event("startup")
def startup():
    global INDEXES_READY
    try:
        create_indexes()
        INDEXES_READY = True
    except PyMongoError:
        # Keep the service alive so /health can correctly report 503 if MongoDB is down.
        INDEXES_READY = False


def parse_date(value: str, label="date") -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")


def parse_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def valid_time(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""))


def trunc_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: int) -> datetime:
    # Pydantic models reject bools, floats, strings and values outside the contract.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def to_ms(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return value


def public_doc(doc: dict) -> dict:
    """Convert BSON datetimes (including nested audit fields) to epoch milliseconds."""
    def convert(v):
        if isinstance(v, datetime): return to_ms(v)
        if isinstance(v, ObjectId): return str(v)
        if isinstance(v, list): return [convert(x) for x in v]
        if isinstance(v, dict): return {k: convert(x) for k, x in v.items() if k != "_id"}
        return v
    return convert(doc)


def shift_start_dt(attendance_day: str, shift_start: str) -> datetime:
    d = parse_date(attendance_day)
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(d, time(h, m), IST).astimezone(UTC).replace(microsecond=0)


def shift_end_dt(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    d = parse_date(attendance_day)
    sh, sm = map(int, shift_start.split(":")); eh, em = map(int, shift_end.split(":"))
    if (eh, em) <= (sh, sm): d += timedelta(days=1)
    return datetime.combine(d, time(eh, em), IST).astimezone(UTC).replace(microsecond=0)


def attendance_day_for(ts: datetime, emp: dict) -> str:
    local = ts.astimezone(IST)
    d = local.date()
    start_hm, end_hm = emp["shift_start"], emp["shift_end"]
    if end_hm <= start_hm and local.strftime("%H:%M") < end_hm:
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    start = shift_start_dt(attendance_day, shift_start)
    elapsed = int((punch_in - start).total_seconds() // 60)
    return elapsed if (punch_in - start).total_seconds() > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    end = shift_end_dt(attendance_day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if (punch_out - end).total_seconds() >= 1800 else 0


def recompute(doc: dict, emp: dict) -> dict:
    status = doc.get("status")
    if status in {"ABSENT", "LEAVE"}:
        doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        return doc
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    if pin is None: raise HTTPException(422, "presence status requires punch_in")
    pin = trunc_second(pin); doc["punch_in"] = pin
    actual_day = pin.astimezone(IST).date().isoformat()
    if attendance_day_for(pin, emp) != doc["date"]:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    doc["late_minutes"] = compute_late_minutes(pin, emp["shift_start"], doc["date"])
    if pout is None:
        doc.update(work_hours=None, overtime_minutes=0, half_day=False)
        return doc
    pout = trunc_second(pout); doc["punch_out"] = pout
    seconds = (pout - pin).total_seconds()
    if seconds <= 0 or seconds > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = compute_work_hours(pin, pout)
    doc["work_hours"] = hours
    doc["overtime_minutes"] = compute_overtime(pout, emp["shift_start"], emp["shift_end"], doc["date"])
    doc["half_day"] = hours < 4.50
    return doc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


def validate_ms(v):
    if v is None: return v
    if type(v) is not int or not 100000000000 <= v <= 4102444800000:
        raise ValueError("must be an epoch-millisecond integer in the supported range")
    return v


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def joined_date(cls, v):
        date.fromisoformat(v)
        if date.fromisoformat(v).isoformat() != v: raise ValueError("joined_on must be YYYY-MM-DD")
        return v
    @model_validator(mode="after")
    def shift_diff(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def ms(cls, v): return validate_ms(v)


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def ms(cls, v): return validate_ms(v)


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def ms(cls, v): return validate_ms(v)
    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for key in ("status", "punch_in", "punch_out"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null when supplied")
        return self


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if status: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    return q


def month_match(month: str):
    start, end = parse_month(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def working_days_expr(start_expr, end_expr):
    # Inclusive weekdays using generated dates; used inside MongoDB pipelines.
    return {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_expr, "endDate": end_expr, "unit": "day"}}, 1]}]}, "as": "i", "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$i"}}}}, "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}


def monthly_employee_pipeline(emp_code: str, month: str, joined_on: Optional[str] = None):
    start, end = parse_month(month)
    date_filter = {"$gte": max(start.isoformat(), joined_on or start.isoformat()), "$lte": end.isoformat()}
    return [
        {"$match": {"emp_code": emp_code, "date": date_filter}},
        {"$addFields": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": ["$_weekday", {"$in": ["$status", list(PRESENCE)]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        {"$project": {"_id": 0, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1}}
    ]


def department_summary_pipeline(month: str, department: Optional[str]):
    start, end = parse_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}, {"$gte": ["$date", "$$joined"]}]}}},
            {"$addFields": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": ["$_weekday", {"$in": ["$status", list(PRESENCE)]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$addFields": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {"present_days": 0, "hours_sum": 0, "hours_count": 0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"}, "hours_sum": {"$sum": "$stats.hours_sum"}, "hours_count": {"$sum": "$stats.hours_count"},
            "late_count": {"$sum": "$stats.late_count"}, "total_late_minutes": {"$sum": "$stats.total_late_minutes"}, "leave_count": {"$sum": "$stats.leave_count"}, "on_duty_count": {"$sum": "$stats.on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$hours_sum", "$hours_count"]}, 100]}, 0.5]}}, 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    start, end = parse_month(month)
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ] + ([{"$match": {"employee.department": department}}] if department is not None else []) + [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]


def trend_pipeline(department: str, start: date, end: date):
    # A collectionless aggregation creates the calendar inside MongoDB, so a
    # department with no attendance documents still gets one row per day.
    first = datetime.combine(start, time.min, UTC)
    day_count = (end - start).days + 1
    return [
        {"$documents": [{"_seed": 1}]},
        {"$set": {"date": {"$map": {"input": {"$range": [0, day_count]}, "as": "offset", "in": {"$dateAdd": {"startDate": first, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$date"},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": [{"$dateFromString": {"dateString": "$joined_on"}}, "$$day"]}}},
            {"$project": {"_id": 0, "emp_code": 1}}
        ], "as": "staff"}},
        {"$set": {"codes": {"$map": {"input": "$staff", "as": "s", "in": "$$s.emp_code"}}, "headcount": {"$size": "$staff"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"dayString": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "UTC"}}, "codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$dayString"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}}
        ], "as": "logs"}},
        {"$set": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]},
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [{"$in": ["$$log.status", list(PRESENCE)]}, {"$cond": ["$$log.half_day", 0.5, 1]}, 0]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]},
            None
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "UTC"}}, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$_avg", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_avg", 10000]}, 0.5]}}, 10000]}]}}},
        {"$sort": {"date": 1}}
    ]


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        global INDEXES_READY
        if not INDEXES_READY:
            create_indexes()
            INDEXES_READY = True
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return public_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_doc(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = trunc_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    d = attendance_day_for(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp["shift_start"], d), "overtime_minutes": 0, "half_day": False, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return public_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = trunc_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    candidates = db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}).sort("punch_in", DESCENDING)
    chosen = None
    for item in candidates:
        if (ts - item["punch_in"]).total_seconds() <= 86400:
            chosen = item; break
    if not chosen: raise HTTPException(404, "no punch-in found")
    if ts <= chosen["punch_in"] or (ts - chosen["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = compute_work_hours(chosen["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], chosen["date"])
    updated = db.attendance_logs.find_one_and_update({"_id": chosen["_id"], "punch_out": None}, {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record already punched out")
    return public_doc(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: parse_date(date_from, "date_from")
    if date_to: parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_doc(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    changes_input = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    if body.status in {"ABSENT", "LEAVE"} and ("punch_in" in changes_input or "punch_out" in changes_input):
        raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
    newdoc = dict(old)
    for key, value in changes_input.items():
        newdoc[key] = from_ms(value) if key in {"punch_in", "punch_out"} and value is not None else value
    newdoc = recompute(newdoc, emp)
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for key in fields:
        before, after = old.get(key), newdoc.get(key)
        if isinstance(before, datetime): before = trunc_second(before)
        if isinstance(after, datetime): after = trunc_second(after)
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, "request does not change the record")
    history = list(old.get("history", []))
    history.append({"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes})
    # Optimistic concurrency: only update if the old history and editable fields still match.
    expected = {"_id": old["_id"], "history": old.get("history", [])}
    for k in fields: expected[k] = old.get(k)
    newdoc["history"] = history
    update_fields = {k: newdoc.get(k) for k in fields + ["history"]}
    updated = db.attendance_logs.find_one_and_update(expected, {"$set": update_fields}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return public_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "unknown employee")
    start, end = parse_month(month)
    working_start = max(start, parse_date(emp["joined_on"]))
    working_days = 0
    if working_start <= end:
        cur = working_start
        while cur <= end:
            if cur.weekday() < 5: working_days += 1
            cur += timedelta(days=1)
    rows = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, month, emp["joined_on"])))
    s = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    pct = None if working_days == 0 else float((Decimal(str(s["present_days"])) * 100 / Decimal(working_days)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    return {"emp_code": emp_code, "month": month, "working_days": working_days, **s, "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start: raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "unknown department")
    rows = list(db.aggregate(trend_pipeline(department, start, end)))
    # A missing date may occur when no attendance documents exist at all; pipeline densifies existing groups.
    # Ensure returned numeric values have the required JSON types.
    for r in rows:
        for key in ("attendance_rate", "moving_avg_7d"):
            if r.get(key) is not None: r[key] = float(Decimal(str(r[key])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        r["present_count"] = float(r.get("present_count", 0))
    return {"department": department, "from": from_date, "to": to_date, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
            emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
            date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
            from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        plan = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if endpoint != "department_trend":
        if not month: raise HTTPException(422, "month is required for this endpoint")
        parse_month(month)
    if endpoint == "employee_monthly":
        if not emp_code: raise HTTPException(422, "emp_code is required")
        emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
        if not emp: raise HTTPException(404, "unknown employee")
        pipeline = monthly_employee_pipeline(emp_code, month, emp["joined_on"])
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if endpoint == "department_summary":
        pipeline = department_summary_pipeline(month, department)
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}
    if endpoint == "late_leaderboard":
        pipeline = leaderboard_pipeline(month, limit, department)
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start or (end-start).days+1 > 92: raise HTTPException(422, "invalid trend range")
    pipeline = trend_pipeline(department, start, end)
    plan = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "employees", "explain": plan}
