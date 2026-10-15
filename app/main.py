"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rounding
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC BSON datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return None
    sec = (ms // 1000)
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def round_half_up_2(val: Optional[float | Decimal]) -> Optional[float]:
    """Round to 2 decimal places half-up (R8)."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float | Decimal]) -> Optional[float]:
    """Round to 4 decimal places half-up (R8)."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def determine_attendance_date_and_shift(
    punch_in_utc: datetime, shift_start: str, shift_end: str
) -> tuple[str, datetime, datetime]:
    """
    R1: Compute attendance date (YYYY-MM-DD IST) and shift start/end datetimes (in IST).
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end
    belongs to the shift that started the previous day, and shift ends on the next day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    ist_time_str = punch_in_ist.strftime("%H:%M:%S")

    is_overnight = shift_end <= shift_start
    if is_overnight and ist_time_str < f"{shift_end}:00":
        att_date = ist_date - timedelta(days=1)
    else:
        att_date = ist_date

    h_s, m_s = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h_s, m_s, 0, tzinfo=IST)

    h_e, m_e = map(int, shift_end.split(":"))
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, h_e, m_e, 0, tzinfo=IST)

    return att_date.isoformat(), shift_start_dt, shift_end_dt


def compute_late_minutes(punch_in_utc: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    """
    diff_sec = (punch_in_utc - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def compute_overtime(punch_out_utc: datetime, shift_end_dt: datetime) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    diff_sec = (punch_out_utc - shift_end_dt).total_seconds()
    if diff_sec < 0:
        return 0
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """
    R5: half_day is true when the rounded work_hours < 4.50.
    """
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days (Mon-Fri) in month_str (YYYY-MM), counted from joined_on for mid-month joiners.
    """
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    m_start = date(y, m, 1)
    m_end = date(y, m, num_days)

    emp_join = date.fromisoformat(joined_on_str)
    if emp_join > m_end:
        return 0

    start_d = max(m_start, emp_join)
    count = 0
    curr = start_d
    while curr <= m_end:
        if curr.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        curr += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB answers a ping, else 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database ping failed")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at,
    }

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_in_dt = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    att_date, shift_start_dt, _ = determine_attendance_date_and_shift(
        punch_in_dt, emp["shift_start"], emp["shift_end"]
    )

    late_mins = compute_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_out_dt = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    # Find the most recent record whose punch_in <= punch_out_dt
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )

    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_dt = rec["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    _, _, shift_end_dt = determine_attendance_date_and_shift(
        punch_in_dt, emp["shift_start"], emp["shift_end"]
    )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime(punch_out_dt, shift_end_dt)
    h_day = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        try:
            date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_from")

    if date_to is not None:
        try:
            date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422, detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE"
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = from_epoch_ms(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if body.punch_out is not None:
            new_punch_out = from_epoch_ms(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

        # R1: punch_in must stay on the record's attendance date
        calc_date, shift_start_dt, shift_end_dt = determine_attendance_date_and_shift(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, not {date}",
            )

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, shift_end_dt)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, shift_start_dt)

    fields_to_check = {
        "status": (rec.get("status"), target_status),
        "punch_in": (rec.get("punch_in"), new_punch_in),
        "punch_out": (rec.get("punch_out"), new_punch_out),
        "work_hours": (rec.get("work_hours"), new_work_hours),
        "late_minutes": (rec.get("late_minutes", 0), new_late_minutes),
        "overtime_minutes": (rec.get("overtime_minutes", 0), new_overtime_minutes),
        "half_day": (rec.get("half_day", False), new_half_day),
    }

    changes = {}
    for f, (old_val, new_val) in fields_to_check.items():
        if old_val != new_val:
            changes[f] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    current_history = rec.get("history", [])
    query = {
        "_id": rec["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current_history)]},
    }
    update = {
        "$set": {
            "status": target_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "d": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$addFields": {
                "is_weekday": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_presence"]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late = int(data.get("total_late_minutes", 0))
        total_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up_2(
            Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal(100)
        )
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up_2(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "log_date_dt": {
                    "$cond": [
                        {"$ne": ["$logs.date", None]},
                        {"$dateFromString": {"dateString": "$logs.date"}},
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": "$log_date_dt"}, 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    formatted_items = []
    for item in items:
        formatted_items.append(
            {
                "department": item["department"],
                "headcount": item["headcount"],
                "present_days": round_half_up_2(item["present_days"]),
                "avg_work_hours": round_half_up_2(item["avg_work_hours"]),
                "late_count": item["late_count"],
                "total_late_minutes": item["total_late_minutes"],
                "leave_count": item["leave_count"],
                "on_duty_count": item["on_duty_count"],
            }
        )

    return {"month": month, "items": formatted_items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if t_d < f_d:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")

    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if department has no employees
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(f_d.year, f_d.month, f_d.day, tzinfo=timezone.utc)
    to_dt_exclusive = datetime(t_d.year, t_d.month, t_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"$ifNull": ["$logs.date", from_date]},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_exclusive],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    formatted_items = []
    for item in items:
        rate = round_half_up_4(item["attendance_rate"]) if item["attendance_rate"] is not None else None
        mavg = round_half_up_4(item["moving_avg_7d"]) if item["moving_avg_7d"] is not None else None
        formatted_items.append(
            {
                "date": item["date"],
                "is_working_day": item["is_working_day"],
                "headcount": item["headcount"],
                "present_count": round_half_up_2(item["present_count"]),
                "late_count": item["late_count"],
                "attendance_rate": rate,
                "moving_avg_7d": mavg,
            }
        )

    return {"department": department, "items": formatted_items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
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

        p = page or 1
        ps = page_size or 20
        skip = (p - 1) * ps
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": ps,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query param: emp_code")
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query param: month")

        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {"$addFields": {"d": {"$dateFromString": {"dateString": "$date"}}}},
            {
                "$addFields": {
                    "is_weekday": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", "$is_presence"]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query param: month")

        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "log_date_dt": {
                        "$cond": [
                            {"$ne": ["$logs.date", None]},
                            {"$dateFromString": {"dateString": "$logs.date"}},
                            None,
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$lte": [{"$isoDayOfWeek": "$log_date_dt"}, 5]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "work_hours_list": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE",
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$logs", None]},
                                {"$ifNull": ["$logs.late_minutes", 0]},
                                0,
                            ]
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$eq": ["$logs.status", "LEAVE"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$eq": ["$logs.status", "ON_DUTY"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query param: month")

        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        lim = limit or 10
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": lim}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ]
        )
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query param: department")
        if not from_date:
            raise HTTPException(status_code=422, detail="Missing required query param: from")
        if not to_date:
            raise HTTPException(status_code=422, detail="Missing required query param: to")

        try:
            f_d = date.fromisoformat(from_date)
            t_d = date.fromisoformat(to_date)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if t_d < f_d:
            raise HTTPException(status_code=422, detail="'to' must be >= 'from'")

        from_dt = datetime(f_d.year, f_d.month, f_d.day, tzinfo=timezone.utc)
        to_dt_exclusive = datetime(t_d.year, t_d.month, t_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", from_date]},
                                        {"$lte": ["$date", to_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": {"$ifNull": ["$logs.date", from_date]},
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
            {
                "$densify": {
                    "field": "d",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_exclusive],
                    },
                }
            },
            {
                "$addFields": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$project": {"_id": 0, "joined_on": 1}},
                    ],
                    "as": "dept_emps",
                }
            },
            {
                "$addFields": {
                    "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$dept_emps",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    },
                }
            },
        ]
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    raise HTTPException(status_code=422, detail="Invalid endpoint")


DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "dashboard.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def get_dashboard():
    if os.path.exists(DASHBOARD_FILE):
        with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>HROne Attendance & Analytics API</h1><p><a href='/docs'>Swagger API Docs</a></p>")
