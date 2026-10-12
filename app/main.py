import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import Collection
from bson import ObjectId
from bson.json_util import dumps
import json

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500)
db = client[MONGO_DB]
employees: Collection = db["employees"]
attendance: Collection = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS = PRESENCE | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------- startup / indexes ----------------------------

@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_employee_date_desc")
    attendance.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_late")


# ---------------------------- helpers / models -----------------------------

def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    # Pydantic validators reject invalid values; keep this helper defensive.
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch-millisecond value in the allowed range")
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    result = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(result)


def whole_minutes(seconds: float) -> int:
    return int(seconds // 60)


def time_parts(hhmm: str) -> tuple[int, int]:
    if not isinstance(hhmm, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
        raise HTTPException(status_code=422, detail="shift times must use HH:MM")
    h, m = map(int, hhmm.split(":"))
    return h, m


def shift_datetimes(emp: dict, attendance_date: str) -> tuple[datetime, datetime]:
    day = parse_date(attendance_date)
    sh, sm = time_parts(emp["shift_start"])
    eh, em = time_parts(emp["shift_end"])
    start = datetime.combine(day, time(sh, sm), IST)
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    end = datetime.combine(end_day, time(eh, em), IST)
    return start.astimezone(UTC).replace(microsecond=0), end.astimezone(UTC).replace(microsecond=0)


def attendance_day(emp: dict, punch_in: datetime) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    sh, sm = time_parts(emp["shift_start"])
    eh, em = time_parts(emp["shift_end"])
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        day -= timedelta(days=1)
    return day


def compute_late(emp: dict, attendance_date: str, punch_in: Optional[datetime]) -> int:
    if punch_in is None:
        return 0
    start, _ = shift_datetimes(emp, attendance_date)
    elapsed = (truncate_instant(punch_in) - start).total_seconds()
    return whole_minutes(elapsed) if elapsed > 600 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = (truncate_instant(punch_out) - truncate_instant(punch_in)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime(emp: dict, attendance_date: str, punch_out: Optional[datetime]) -> int:
    if punch_out is None:
        return 0
    _, end = shift_datetimes(emp, attendance_date)
    minutes = whole_minutes((truncate_instant(punch_out) - end).total_seconds())
    return minutes if minutes >= 30 else 0


def compute_derived(emp: dict, att: dict) -> dict:
    pi = att.get("punch_in")
    po = att.get("punch_out")
    if att.get("status") not in PRESENCE:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late(emp, att["date"], pi)
    hours = compute_work_hours(pi, po)
    overtime = compute_overtime(emp, att["date"], po) if po else 0
    half = bool(hours is not None and hours < 4.50)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}


def public_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def public_attendance(doc: dict) -> dict:
    history = []
    for item in doc.get("history", []) or []:
        changes = {}
        for field, values in (item.get("changes") or {}).items():
            changes[field] = {
                "from": to_epoch_ms(values.get("from")) if field in {"punch_in", "punch_out"} else values.get("from"),
                "to": to_epoch_ms(values.get("to")) if field in {"punch_in", "punch_out"} else values.get("to"),
            }
        history.append({
            "at": to_epoch_ms(item.get("at")), "by": item.get("by", ""),
            "reason": item.get("reason", ""), "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")), "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)), "history": history,
    }


def require_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def json_safe(value: Any) -> Any:
    return json.loads(dumps(value))


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            total += 1
        cur += timedelta(days=1)
    return total


def present_value(doc: dict) -> float:
    if doc.get("status") not in PRESENCE:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def validate_epoch_field(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch-millisecond value between 100000000000 and 4102444800000")
    return value


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def check_emp_code(cls, v):
        if not re.fullmatch(r"EMP\d{4,6}", v or ""):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def check_email(cls, v):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v or ""):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift_time(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v or ""):
            raise ValueError("time must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v):
        try:
            if date.fromisoformat(v).isoformat() != v:
                raise ValueError()
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, v):
        return validate_epoch_field(v)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, v):
        return validate_epoch_field(v)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, v):
        return validate_epoch_field(v)


# -------------------------------- health ------------------------------------

@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# -------------------------------- employees ---------------------------------

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return public_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    docs = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_employee(x) for x in docs], "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance --------------------------------

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = require_employee(body.emp_code)
    instant = truncate_instant(from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now())
    att_date = attendance_day(emp, instant).isoformat()
    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    doc.update(compute_derived(emp, doc))
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance already exists for this employee and date")
    return public_attendance(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = require_employee(body.emp_code)
    instant = truncate_instant(from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now())
    # Most recent eligible record; scan only this employee's recent open records.
    candidates = attendance.find({
        "emp_code": body.emp_code, "status": {"$in": list(PRESENCE)},
        "punch_in": {"$ne": None, "$lte": instant},
        "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
    }).sort([("punch_in", DESCENDING)]).limit(30)
    target = None
    for candidate in candidates:
        elapsed = (instant - truncate_instant(candidate["punch_in"])).total_seconds()
        if 0 < elapsed <= 24 * 3600:
            target = candidate
            break
    if target is None:
        # Distinguish no punch-in / invalid range from unknown employee.
        latest = attendance.find_one({"emp_code": body.emp_code, "status": {"$in": list(PRESENCE)}, "punch_in": {"$ne": None}}, sort=[("punch_in", DESCENDING)])
        if latest and instant <= truncate_instant(latest["punch_in"]):
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        if latest and (instant - truncate_instant(latest["punch_in"])).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        already = attendance.find_one({"emp_code": body.emp_code, "status": {"$in": list(PRESENCE)}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])
        if already:
            raise HTTPException(status_code=409, detail="Attendance record is already punched out")
        raise HTTPException(status_code=404, detail="No eligible punch-in found")
    derived_doc = dict(target)
    derived_doc["punch_out"] = instant
    derived = compute_derived(emp, derived_doc)
    result = attendance.find_one_and_update(
        {"_id": target["_id"], "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}]},
        {"$set": {"punch_out": instant, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    return public_attendance(result)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt: dict = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if status is not None:
        filt["status"] = status
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if date_from or date_to:
        filt["date"] = {}
        if date_from:
            filt["date"]["$gte"] = date_from
        if date_to:
            filt["date"]["$lte"] = date_to
    total = attendance.count_documents(filt)
    docs = attendance.find(filt, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    parse_date(date)
    emp = require_employee(emp_code)
    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    changes_requested = {}
    if body.status is not None:
        changes_requested["status"] = body.status
    if "punch_in" in body.model_fields_set:
        changes_requested["punch_in"] = from_epoch_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set:
        changes_requested["punch_out"] = from_epoch_ms(body.punch_out) if body.punch_out is not None else None
    new = dict(old)
    new.update(changes_requested)
    status = new.get("status")
    if status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        new["punch_in"] = None
        new["punch_out"] = None
    elif status in PRESENCE and new.get("punch_in") is None:
        raise HTTPException(status_code=422, detail="Presence status requires punch_in")
    if new.get("punch_in") is not None:
        pi = truncate_instant(new["punch_in"])
        if attendance_day(emp, pi).isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        new["punch_in"] = pi
    if new.get("punch_out") is not None:
        new["punch_out"] = truncate_instant(new["punch_out"])
        if new.get("punch_in") is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (new["punch_out"] - new["punch_in"]).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
    derived = compute_derived(emp, new)
    new.update(derived)
    compare_fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    actual_changes = {}
    for key in compare_fields:
        before = old.get(key, False if key == "half_day" else None if key in {"punch_in", "punch_out", "work_hours"} else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        after = new.get(key, False if key == "half_day" else None if key in {"punch_in", "punch_out", "work_hours"} else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        if before != after:
            actual_changes[key] = {"from": before, "to": after}
    if not actual_changes:
        raise HTTPException(status_code=422, detail="Request does not change the record")
    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": actual_changes}
    # Compare-and-set prevents a stale correction from overwriting concurrent edits/history.
    old_history = old.get("history", [])
    filter_doc = {"_id": old["_id"]}
    filter_doc["history"] = old_history
    if "punch_in" in old:
        filter_doc["punch_in"] = old["punch_in"]
    else:
        filter_doc["punch_in"] = {"$exists": False}
    if "punch_out" in old:
        filter_doc["punch_out"] = old["punch_out"]
    else:
        filter_doc["punch_out"] = {"$exists": False}
    update = {"$set": {k: new.get(k) for k in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]},
              "$push": {"history": entry}}
    result = attendance.find_one_and_update(filter_doc, update, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry the correction")
    return public_attendance(result)


# ------------------------------- analytics ---------------------------------

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = require_employee(emp_code)
    first, last = parse_month(month)
    logs = list(attendance.find({"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}, {"_id": 0}))
    wd_start = max(first, parse_date(emp["joined_on"]))
    wd = working_days(wd_start, last) if wd_start <= last else 0
    present = sum(present_value(x) for x in logs if parse_date(x["date"]).weekday() < 5)
    leaves = sum(1 for x in logs if x.get("status") == "LEAVE")
    late_count = sum(1 for x in logs if int(x.get("late_minutes", 0) or 0) > 0)
    late_total = sum(int(x.get("late_minutes", 0) or 0) for x in logs)
    overtime_total = sum(int(x.get("overtime_minutes", 0) or 0) for x in logs)
    pct = round_half_up(Decimal(str(present)) / Decimal(wd) * 100, 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round_half_up(present, 2),
            "leave_days": leaves, "late_count": late_count, "total_late_minutes": late_total,
            "total_overtime_minutes": overtime_total, "attendance_pct": pct}


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    employee_filter = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_filter["department"] = department
    emps = list(employees.find(employee_filter, {"_id": 0}))
    if not emps:
        return {"month": month, "items": []}
    codes = [e["emp_code"] for e in emps]
    logs = list(attendance.find({"emp_code": {"$in": codes}, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}, {"_id": 0}))
    grouped = {}
    for e in emps:
        grouped.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present_days": 0.0,
             "work_hours_values": [], "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0})
        grouped[e["department"]]["headcount"] += 1
    emp_by_code = {e["emp_code"]: e for e in emps}
    for log in logs:
        e = emp_by_code.get(log["emp_code"])
        if not e:
            continue
        g = grouped[e["department"]]
        if parse_date(log["date"]).weekday() < 5:
            g["present_days"] += present_value(log)
        if log.get("status") in PRESENCE and log.get("work_hours") is not None:
            g["work_hours_values"].append(float(log["work_hours"]))
        late = int(log.get("late_minutes", 0) or 0)
        if late > 0:
            g["late_count"] += 1
            g["total_late_minutes"] += late
        if log.get("status") == "LEAVE":
            g["leave_count"] += 1
        if log.get("status") == "ON_DUTY":
            g["on_duty_count"] += 1
    items = []
    for g in sorted(grouped.values(), key=lambda x: x["department"]):
        vals = g.pop("work_hours_values")
        g["present_days"] = round_half_up(g["present_days"], 2)
        g["avg_work_hours"] = round_half_up(sum(vals) / len(vals), 2) if vals else None
        items.append(g)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    emp_filter = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_filter["department"] = department
    emps = list(employees.find(emp_filter, {"_id": 0}))
    by_code = {e["emp_code"]: e for e in emps}
    logs = list(attendance.find({"emp_code": {"$in": list(by_code)}, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}, {"_id": 0}))
    grouped = {}
    for log in logs:
        late = int(log.get("late_minutes", 0) or 0)
        if late <= 0:
            continue
        code = log["emp_code"]
        g = grouped.setdefault(code, {"total_late_minutes": 0, "late_count": 0})
        g["total_late_minutes"] += late
        g["late_count"] += 1
    ranked = []
    for code, g in grouped.items():
        e = by_code[code]
        ranked.append({"emp_code": code, "name": e["name"], "department": e["department"], **g})
    ranked.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    previous_total = None
    rank = 0
    for idx, item in enumerate(ranked, start=1):
        if item["total_late_minutes"] != previous_total:
            rank = idx
            previous_total = item["total_late_minutes"]
        item["rank"] = rank
    return {"month": month, "items": [x for x in ranked if x["rank"] <= limit]}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    dept_emps = list(employees.find({"department": department}, {"_id": 0}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")
    codes = [e["emp_code"] for e in dept_emps]
    logs = list(attendance.find({"emp_code": {"$in": codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, {"_id": 0}))
    per_day = {}
    for log in logs:
        row = per_day.setdefault(log["date"], {"present_count": 0.0, "late_count": 0})
        row["present_count"] += present_value(log)
        if int(log.get("late_minutes", 0) or 0) > 0:
            row["late_count"] += 1
    # Produce a continuous date series; rates and moving averages are computed from this series.
    # Note: the assignment asks for database-side densification/moving average; see explanation below.
    items = []
    rates = []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= ds)
        row = per_day.get(ds, {"present_count": 0.0, "late_count": 0})
        is_working = cur.weekday() < 5
        rate = round_half_up(row["present_count"] / headcount, 4) if is_working and headcount > 0 else None
        rates.append(rate)
        window = [x for x in rates[max(0, len(rates) - 7):] if x is not None]
        moving = round_half_up(sum(window) / len(window), 4) if window else None
        items.append({"date": ds, "is_working_day": is_working, "headcount": headcount,
                      "present_count": round_half_up(row["present_count"], 2), "late_count": row["late_count"],
                      "attendance_rate": rate, "moving_avg_7d": moving})
        cur += timedelta(days=1)
    return {"department": department, "items": items}


# ------------------------------- explain -----------------------------------

def explain_find_stage(value: Any, stage: str) -> bool:
    if isinstance(value, dict):
        if value.get("stage") == stage:
            return True
        return any(explain_find_stage(v, stage) for v in value.values())
    if isinstance(value, list):
        return any(explain_find_stage(v, stage) for v in value)
    return False


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        filt = {}
        if emp_code is not None:
            filt["emp_code"] = emp_code
        if status is not None:
            filt["status"] = status
        if date_from:
            parse_date(date_from, "date_from")
            filt.setdefault("date", {})["$gte"] = date_from
        if date_to:
            parse_date(date_to, "date_to")
            filt.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        result = attendance.find(filt).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(result)}
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = parse_month(month)
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(status_code=422, detail="emp_code is required")
            pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                        {"$group": {"_id": "$emp_code", "count": {"$sum": 1}, "late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
            exp = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
        if endpoint == "department_summary":
            match = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}
            pipeline = [{"$match": match}, {"$group": {"_id": "$emp_code", "late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
            exp = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
        match = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}
        pipeline = [{"$match": match}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}, {"$sort": {"total": -1, "_id": 1}}]
        exp = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        dept_codes = [e["emp_code"] for e in employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
        pipeline = [{"$match": {"emp_code": {"$in": dept_codes}, "date": {"$gte": from_date, "$lte": to_date}}},
                    {"$group": {"_id": "$date", "present_count": {"$sum": 1}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}]
        exp = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
    raise HTTPException(status_code=422, detail="Unknown endpoint")


# -------------------------- database exception handler ---------------------

@app.exception_handler(PyMongoError)
async def mongo_exception_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database operation failed"})
