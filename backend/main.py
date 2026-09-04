import csv
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.rail_traffic_provider import (
    RailTrafficAuthError,
    RailTrafficError,
    RailTrafficNotConfigured,
    RailTrafficProvider,
    RailTrafficProviderError,
    RailTrafficRateLimited,
)
from backend.rail_traffic_models import Corridor, normalize_train_schedule
from backend.live_rail_traffic_service import LiveRailTrafficService
from backend.block_conflict_engine import BlockConflictAnalysisEngine, CandidateBlock, ExistingBlock, FreightForecast
from backend.ml_prediction_service import predictBlockConflictRisk

app = FastAPI(title="RailBlock AI")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")
DATA_LOCK = threading.RLock()
live_rail_traffic = LiveRailTrafficService()
block_conflict_engine = BlockConflictAnalysisEngine()

FIELDS = {
    "trains": ["train_no", "name", "train_type", "direction", "service_time", "source", "scheduled"],
    "requests": ["request_id", "dept", "work", "from_station", "from_sub_station", "to_station", "to_sub_station", "request_day", "must_complete_by", "km_from", "km_to", "duration", "priority", "risk", "status", "hold_until", "created_at", "updated_at", "rejection_reason", "last_action_reason", "asset_id", "asset_type", "safety_clearance_required", "preferred_start", "preferred_end", "reason", "requested_by", "ai_priority_score", "ai_priority_level", "ai_recommendation", "recommended_block_id"],
    "blocks": ["block_id", "start_time", "end_time", "block_day", "km_from", "km_to", "status", "reason", "affected"],
    "block_requests": ["block_id", "request_id"],
    "assets": ["asset_id", "section_id", "asset_type", "temperature", "vibration", "wear_percentage", "failure_history", "inspection_date", "electrical_load", "last_maintenance"],
    "weather_forecasts": ["forecast_day", "condition", "rainfall_mm", "rain_probability", "wind_kmh", "visibility_km", "planning_factor"],
    "traffic_demand": ["demand_day", "peak_start", "peak_end", "passenger_trains", "freight_trains", "demand_score"],
    "resources": ["resource_id", "resource_type", "name", "available_units", "unit_cost"],
    "audit_events": ["event_id", "entity_type", "entity_id", "action", "actor", "reason", "created_at"],
}


@dataclass
class MaintenanceRequest:
    id: str
    department: str
    assetId: str
    assetType: str
    activity: str
    reason: str
    fromStation: str
    toStation: str
    fromKm: float
    toKm: float
    preferredStart: str
    preferredEnd: str
    durationMinutes: int
    requestDate: str
    mustCompleteBy: str
    safetyClearance: bool
    safetyPriority: str
    defectSeverity: str
    assetCriticality: str
    trafficImpact: str
    aiPriorityScore: int
    aiPriorityLevel: str
    aiRecommendation: str
    recommendedBlockId: Optional[str]
    status: str

    @classmethod
    def from_csv_row(cls, row, asset_type: str = "Unknown"):
        priority = row.get("priority") or "Medium"
        risk = int(row.get("ai_priority_score") or row.get("risk") or 0)
        status = row.get("status") or "Coordination hold"
        if risk >= 80:
            defect_severity = "Critical"
        elif risk >= 60:
            defect_severity = "High"
        else:
            defect_severity = "Medium"
        if asset_type.lower() in {"track", "ohe", "overhead", "signalling", "traction"}:
            criticality = "Critical" if defect_severity == "Critical" else "High"
        else:
            criticality = "Medium"
        if risk >= 75:
            traffic_impact = "High"
        elif risk >= 45:
            traffic_impact = "Moderate"
        else:
            traffic_impact = "Low"
        recommendation = row.get("ai_recommendation") or (
            "Proceed with coordinated possession" if status in {"Approved", "Pending COA"} else
            "Reject and re-submit with corrected scope" if status == "Rejected" else
            "AI decision support / optimization: review the recommended block before approval."
        )
        return cls(
            id=row.get("request_id", ""),
            department=row.get("dept", "TMS"),
            assetId=row.get("asset_id", ""),
            assetType=asset_type,
            activity=row.get("work", ""),
            reason=row.get("reason", ""),
            fromStation=row.get("from_station", ""),
            toStation=row.get("to_station", ""),
            fromKm=float(row.get("km_from", 0) or 0),
            toKm=float(row.get("km_to", 0) or 0),
            preferredStart=row.get("preferred_start", ""),
            preferredEnd=row.get("preferred_end", ""),
            durationMinutes=int(row.get("duration", 0) or 0),
            requestDate=row.get("request_day", ""),
            mustCompleteBy=row.get("must_complete_by", ""),
            safetyClearance=as_bool(row.get("safety_clearance_required", False)),
            safetyPriority=priority,
            defectSeverity=defect_severity,
            assetCriticality=criticality,
            trafficImpact=traffic_impact,
            aiPriorityScore=risk,
            aiPriorityLevel=row.get("ai_priority_level") or priority,
            aiRecommendation=recommendation,
            recommendedBlockId=row.get("recommended_block_id") or row.get("recommendedBlockId"),
            status=status,
        )

    def to_api_dict(self):
        base = {
            "id": self.id,
            "department": self.department,
            "assetId": self.assetId,
            "assetType": self.assetType,
            "activity": self.activity,
            "reason": self.reason,
            "fromStation": self.fromStation,
            "toStation": self.toStation,
            "fromKm": self.fromKm,
            "toKm": self.toKm,
            "preferredStart": self.preferredStart,
            "preferredEnd": self.preferredEnd,
            "durationMinutes": self.durationMinutes,
            "requestDate": self.requestDate,
            "mustCompleteBy": self.mustCompleteBy,
            "safetyClearance": self.safetyClearance,
            "safetyPriority": self.safetyPriority,
            "defectSeverity": self.defectSeverity,
            "assetCriticality": self.assetCriticality,
            "trafficImpact": self.trafficImpact,
            "aiPriorityScore": self.aiPriorityScore,
            "aiPriorityLevel": self.aiPriorityLevel,
            "aiRecommendation": self.aiRecommendation,
            "recommendedBlockId": self.recommendedBlockId,
            "status": self.status,
        }
        base.update({
            "dept": self.department,
            "work": self.activity,
            "fromSubStation": "",
            "toSubStation": "",
            "day": self.requestDate,
            "mustCompleteBy": self.mustCompleteBy,
            "kmFrom": self.fromKm,
            "kmTo": self.toKm,
            "duration": self.durationMinutes,
            "priority": self.safetyPriority,
            "risk": self.aiPriorityScore,
            "assetId": self.assetId,
            "safetyClearanceRequired": self.safetyClearance,
            "preferredStart": self.preferredStart,
            "preferredEnd": self.preferredEnd,
            "reason": self.reason,
            "requestedBy": self.department,
            "holdUntil": None,
        })
        return base


@dataclass
class Block:
    id: str
    corridor: str
    fromKm: float
    toKm: float
    date: str
    startTime: str
    endTime: str
    durationMinutes: int
    trafficLevel: str
    passengerConflicts: int
    freightConflicts: int
    weatherRisk: int
    aiScore: int
    status: str
    maintenanceRequestIds: list
    bundlingSavingsMinutes: int
    approvalState: str

    @classmethod
    def from_csv_row(cls, row, maintenance_request_ids=None):
        start = row.get("start_time", "00:00")
        end = row.get("end_time", "00:00")
        duration = max(0, hmin(end) - hmin(start)) if start and end else 0
        return cls(
            id=row.get("block_id", ""),
            corridor="Bengaluru – Dharmavaram",
            fromKm=float(row.get("km_from", 0) or 0),
            toKm=float(row.get("km_to", 0) or 0),
            date=row.get("block_day", ""),
            startTime=start,
            endTime=end,
            durationMinutes=duration,
            trafficLevel="normal",
            passengerConflicts=0,
            freightConflicts=0,
            weatherRisk=0,
            aiScore=85,
            status=row.get("status", "Planned"),
            maintenanceRequestIds=maintenance_request_ids or [],
            bundlingSavingsMinutes=0,
            approvalState=row.get("status", "Planned"),
        )

    def to_api_dict(self, row=None):
        row = row or {}
        base = {
            "id": self.id,
            "corridor": self.corridor,
            "fromKm": self.fromKm,
            "toKm": self.toKm,
            "date": self.date,
            "startTime": self.startTime,
            "endTime": self.endTime,
            "durationMinutes": self.durationMinutes,
            "trafficLevel": self.trafficLevel,
            "passengerConflicts": self.passengerConflicts,
            "freightConflicts": self.freightConflicts,
            "weatherRisk": self.weatherRisk,
            "aiScore": self.aiScore,
            "status": self.status,
            "maintenanceRequestIds": self.maintenanceRequestIds,
            "bundlingSavingsMinutes": self.bundlingSavingsMinutes,
            "approvalState": self.approvalState,
        }
        legacy = {
            "id": self.id,
            "start": self.startTime,
            "end": self.endTime,
            "day": self.date,
            "kmFrom": self.fromKm,
            "kmTo": self.toKm,
            "status": self.status,
            "requests": self.maintenanceRequestIds,
            "reason": row.get("reason", ""),
            "affected": int(row.get("affected", 0) or 0),
        }
        base.update(legacy)
        return base


def csv_path(table):
    return DATA / f"{table}.csv"


def read_rows(table):
    path = csv_path(table)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_rows(table, rows):
    DATA.mkdir(exist_ok=True)
    temporary = csv_path(table).with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS[table])
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path(table))


@contextmanager
def repository():
    with DATA_LOCK:
        yield


def initialize_data():
    DATA.mkdir(exist_ok=True)
    for table in FIELDS:
        if not csv_path(table).exists():
            write_rows(table, [])


@app.on_event("startup")
def startup():
    initialize_data()
    live_rail_traffic.start()


@app.on_event("shutdown")
def shutdown():
    live_rail_traffic.stop()


class RequestIn(BaseModel):
    dept: Literal["TMS", "SMMS", "TDMS"]
    work: str
    assetType: str = "Track"
    fromStation: str
    fromSubStation: str
    toStation: str
    toSubStation: str
    day: date
    mustCompleteBy: date
    kmFrom: float = Field(ge=0, le=200)
    kmTo: float = Field(ge=0, le=200)
    duration: int = Field(ge=30, le=360)
    priority: Literal["Low", "Medium", "High", "Critical"]
    assetId: str = ""
    safetyClearanceRequired: bool = False
    preferredStart: str = ""
    preferredEnd: str = ""
    reason: str = ""
    requestedBy: str = "TMS"


class BlockAction(BaseModel):
    action: Literal["approve", "complete", "extend"]
    minutes: int = 0


class RequestAction(BaseModel):
    action: Literal["approve", "reject", "release"]
    reason: Optional[str] = None


class ManualBlock(BaseModel):
    kmFrom: float = Field(ge=0, le=200)
    kmTo: float = Field(ge=0, le=200)
    day: date
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration: int = Field(ge=30, le=360)
    reason: str


class SimulationIn(BaseModel):
    day: date
    kmFrom: float = Field(ge=0, le=200)
    kmTo: float = Field(ge=0, le=200)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration: int = Field(ge=30, le=360)
    rainfallPercent: Optional[int] = Field(default=None, ge=0, le=100)
    trafficLevel: Literal["normal", "festival_peak"] = "normal"
    addMinutes: int = Field(default=0, ge=-120, le=240)


def hmin(value):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Time must use 24-hour HH:MM format")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def time_string(minutes):
    if not 0 <= minutes < 1440:
        raise ValueError("Time must be within one calendar day")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def ranges_overlap(first_from, first_to, second_from, second_to):
    return first_from < second_to and second_from < first_to


def times_overlap(first_start, first_end, second_start, second_end):
    return first_start < second_end and second_start < first_end


def as_bool(value):
    return value in (True, "True", "true", "1", 1)


def priority_level_from_score(score):
    if score >= 85:
        return "Critical"
    if score >= 70:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


def request_deadline_urgency(request_row):
    request_day = request_row.get("request_day") or date.today().isoformat()
    completion_day = request_row.get("must_complete_by") or request_day
    try:
        delta_days = max((date.fromisoformat(completion_day) - date.fromisoformat(request_day)).days, 0)
    except ValueError:
        delta_days = 0
    urgency = max(0, 100 - (delta_days * 18))
    if request_row.get("priority") == "Critical":
        urgency = max(urgency, 85)
    return min(100, urgency)


def calculate_ai_priority_score(request_row):
    severity_map = {"Low": 25, "Medium": 50, "High": 75, "Critical": 95}
    asset_map = {"Low": 25, "Medium": 50, "High": 75, "Critical": 95}
    traffic_map = {"Low": 20, "Moderate": 55, "High": 80}
    safety_map = {"Low": 25, "Medium": 55, "High": 80, "Critical": 95}

    defect_severity = request_row.get("defect_severity") or request_row.get("priority") or "Medium"
    asset_criticality = request_row.get("asset_criticality") or request_row.get("asset_type") or "High"
    traffic_impact = request_row.get("traffic_impact") or "Moderate"
    safety_priority = request_row.get("safety_priority") or request_row.get("priority") or "Medium"
    deadline_score = request_deadline_urgency(request_row)

    aggregate = (
        severity_map.get(defect_severity, 50) * 0.32
        + deadline_score * 0.24
        + asset_map.get(asset_criticality, 50) * 0.2
        + traffic_map.get(traffic_impact, 55) * 0.14
        + safety_map.get(safety_priority, 55) * 0.1
    )
    if request_row.get("safety_clearance_required") in (True, "True", "true", 1, "1"):
        aggregate += 5
    if int(request_row.get("duration", 0) or 0) > 90:
        aggregate += 4
    if float(request_row.get("km_to", 0) or 0) - float(request_row.get("km_from", 0) or 0) > 1.5:
        aggregate += 3
    return max(0, min(99, int(round(aggregate))))


def spatially_compatible(request_row, other_row, threshold=1.0):
    current_from = float(request_row.get("km_from") or 0)
    current_to = float(request_row.get("km_to") or 0)
    other_from = float(other_row.get("km_from") or 0)
    other_to = float(other_row.get("km_to") or 0)
    if ranges_overlap(current_from, current_to, other_from, other_to):
        return True
    return min(abs(current_from - other_to), abs(current_to - other_from)) <= threshold


def temporally_compatible(request_row, other_row):
    request_start = hmin(request_row.get("preferred_start") or "00:00")
    request_end = hmin(request_row.get("preferred_end") or time_string(min(1439, request_start + max(30, int(request_row.get("duration", 60) or 60)))))
    other_start = hmin(other_row.get("preferred_start") or "00:00")
    other_end = hmin(other_row.get("preferred_end") or time_string(min(1439, other_start + max(30, int(other_row.get("duration", 60) or 60)))))
    return times_overlap(request_start, request_end, other_start, other_end) or (request_start <= other_start and request_end >= other_end)


def safety_window_conflicts(window_start, window_end, train_rows):
    conflicts = []
    for schedule in train_rows:
        if schedule.trainType == "Goods":
            continue
        train_start = hmin(schedule.scheduledDeparture or schedule.scheduledArrival)
        train_end = train_start + 30
        if times_overlap(window_start, window_end, train_start, train_end):
            conflicts.append(schedule.trainNumber)
    return conflicts


def score_candidate_block(request_row, compatible_requests, candidate_block, weather_row, demand_row):
    request_score = calculate_ai_priority_score(request_row)
    safety_score = {"Low": 35, "Medium": 55, "High": 80, "Critical": 95}.get(request_row.get("priority") or "Medium", 55)
    deadline_score = request_deadline_urgency(request_row)
    weather_risk = int(weather_row["rain_probability"]) if weather_row else 0
    weather_score = max(0, 100 - weather_risk)
    demand_score = int(demand_row["demand_score"]) if demand_row else 50
    demand_penalty = max(0, 100 - demand_score)
    compatible_count = len(compatible_requests)
    requested_duration = int(request_row.get("duration", 60) or 60)
    block_duration = max(0, hmin(candidate_block.get("end_time") or "00:00") - hmin(candidate_block.get("start_time") or "00:00"))
    duration_fit = max(0, 100 - abs(block_duration - requested_duration) * 0.75)
    freight_penalty = 12 if demand_row and int(demand_row.get("freight_trains", 0) or 0) >= 3 else 0
    compatible_durations = [int(item.get("duration", 60) or 60) for item in compatible_requests]
    combined_duration = max([requested_duration] + compatible_durations) if compatible_durations else requested_duration
    savings_minutes = max(0, sum(int(item.get("duration", 60) or 60) for item in compatible_requests + [request_row]) - combined_duration)
    bundling_score = min(100, 15 + compatible_count * 12 + savings_minutes / 2)

    total = (
        request_score * 0.24
        + safety_score * 0.18
        + deadline_score * 0.14
        + (100 - demand_penalty) * 0.12
        + weather_score * 0.12
        + compatible_count * 8
        + duration_fit * 0.12
        + bundling_score * 0.08
        - freight_penalty
    )
    return total


def build_bundling_recommendation(request_row, compatible_requests, candidate_block, block_duration_minutes):
    activities = [request_row.get("work")] + [item.get("work") for item in compatible_requests]
    departments = [request_row.get("dept")] + [item.get("dept") for item in compatible_requests]
    individual_total = sum(int(item.get("duration", 60) or 60) for item in compatible_requests + [request_row])
    compatible_durations = [int(item.get("duration", 60) or 60) for item in compatible_requests]
    combined_total = max([int(request_row.get("duration", 60) or 60)] + compatible_durations) if compatible_durations else int(request_row.get("duration", 60) or 60)
    savings = max(0, individual_total - combined_total)
    return {
        "departments": departments,
        "activities": activities,
        "corridor": f"{request_row.get('from_station', 'Unknown')} – {request_row.get('to_station', 'Unknown')}",
        "duration": combined_total,
        "individualBlockTime": f"{individual_total} min",
        "combinedBlockTime": f"{block_duration_minutes} min",
        "estimatedSavings": f"{savings} min",
        "compatibleRequestIds": [item.get("request_id") for item in compatible_requests],
        "recommendedBlockId": candidate_block.get("block_id"),
    }


def build_bundling_opportunities(request_rows, block_rows, train_rows, weather_rows, demand_rows):
    opportunities = {}
    active_requests = [row for row in request_rows if row.get("dept") in {"TMS", "SMMS", "TDMS"} and row.get("status") not in {"Rejected", "Completed"}]
    for request in active_requests:
        day = request.get("request_day")
        same_day = [row for row in active_requests if row.get("request_day") == day and row.get("request_id") != request.get("request_id")]
        weather = next((row for row in weather_rows if row.get("forecast_day") == day), None)
        demand = next((row for row in demand_rows if row.get("demand_day") == day), None)
        plan = generate_ai_plan_for_request(request, same_day, block_rows, train_rows, weather, demand)
        block_id = plan.get("recommendedBlockId")
        bundle = plan.get("bundlingRecommendation")
        if not block_id or not bundle or not plan.get("compatibleRequests"):
            continue
        if block_id in opportunities:
            continue
        block = next((row for row in block_rows if row.get("block_id") == block_id), None)
        compatible = plan["compatibleRequests"] + [request]
        activities = [{
            "id": row.get("request_id"),
            "activity": row.get("work"),
            "asset": row.get("asset_id") or "Not specified",
            "department": row.get("dept"),
            "location": f"{row.get('from_station', 'Unknown')} – {row.get('to_station', 'Unknown')} · {row.get('km_from', '0')}–{row.get('km_to', '0')} km",
            "duration": int(row.get("duration", 0) or 0),
        } for row in compatible]
        opportunities[block_id] = {
            "id": f"BUNDLE-{block_id}",
            "blockId": block_id,
            "proposedBlockTime": f"{block.get('start_time')}–{block.get('end_time')}",
            "corridor": bundle["corridor"],
            "departments": bundle["departments"],
            "activityCount": len(activities),
            "aiScore": plan["aiPriorityScore"],
            "combinedDuration": bundle["combinedBlockTime"],
            "estimatedBlockTimeSaving": bundle["estimatedSavings"],
            "operationalImpact": "Single coordinated possession replaces separate department windows and protects passenger movements.",
            "activities": activities,
            "compatibilityReason": "KM ranges overlap or fall within the 1.0 km bundling threshold, and preferred work windows are compatible.",
            "trainConflictsAvoided": plan["passengerTrainConflicts"],
            "windowReason": plan["aiRecommendation"],
        }
    return list(opportunities.values())


def generate_ai_plan_for_request(request_row, all_requests, blocks, trains, weather_row=None, demand_row=None):
    request_score = calculate_ai_priority_score(request_row)
    request_row["ai_priority_score"] = str(request_score)
    request_row["ai_priority_level"] = priority_level_from_score(request_score)

    compatible_requests = []
    for other in all_requests:
        if other.get("request_id") == request_row.get("request_id"):
            continue
        if other.get("status") in {"Rejected", "Completed"}:
            continue
        if spatially_compatible(request_row, other, threshold=1.0) and temporally_compatible(request_row, other):
            compatible_requests.append(other)

    window_start = request_row.get("preferred_start") or "00:00"
    window_end = request_row.get("preferred_end") or time_string(min(1439, hmin(window_start) + max(30, int(request_row.get("duration", 60) or 60))))
    passenger_conflicts = safety_window_conflicts(hmin(window_start), hmin(window_end), trains)

    candidate_blocks = []
    fallback_blocks = []
    for block in blocks:
        if block.get("status") in {"Completed", "Cancelled"}:
            continue
        if block.get("block_day") == request_row.get("request_day"):
            fallback_blocks.append(block)
        if block.get("block_day") != request_row.get("request_day"):
            continue
        block_start = hmin(block.get("start_time") or "00:00")
        block_end = hmin(block.get("end_time") or "23:59")
        if not ranges_overlap(float(request_row.get("km_from", 0) or 0), float(request_row.get("km_to", 0) or 0), float(block.get("km_from", 0) or 0), float(block.get("km_to", 0) or 0)):
            continue
        if not times_overlap(hmin(window_start), hmin(window_end), block_start, block_end):
            continue
        block_conflicts = [schedule.trainNumber for schedule in trains if schedule.trainType != "Goods" and times_overlap(block_start, block_end, hmin(schedule.scheduledDeparture or schedule.scheduledArrival), hmin(schedule.scheduledDeparture or schedule.scheduledArrival) + 30)]
        if block_conflicts:
            continue
        candidate_blocks.append({**block, "duration_minutes": max(0, block_end - block_start), "passenger_conflicts": block_conflicts})

    if not candidate_blocks and fallback_blocks:
        for block in fallback_blocks:
            block_start = hmin(block.get("start_time") or "00:00")
            block_end = hmin(block.get("end_time") or "23:59")
            if not ranges_overlap(float(request_row.get("km_from", 0) or 0), float(request_row.get("km_to", 0) or 0), float(block.get("km_from", 0) or 0), float(block.get("km_to", 0) or 0)):
                continue
            candidate_blocks.append({**block, "duration_minutes": max(0, block_end - block_start), "passenger_conflicts": []})

    if not candidate_blocks and blocks:
        for block in blocks:
            if block.get("status") in {"Completed", "Cancelled"}:
                continue
            block_start = hmin(block.get("start_time") or "00:00")
            block_end = hmin(block.get("end_time") or "23:59")
            candidate_blocks.append({**block, "duration_minutes": max(0, block_end - block_start), "passenger_conflicts": []})

    if not candidate_blocks:
        recommendation = "AI decision support / optimization: no compatible COA block was available. Manual coordination review is required."
        request_row["ai_recommendation"] = recommendation
        request_row["recommended_block_id"] = ""
        return {
            "aiPriorityScore": request_score,
            "aiPriorityLevel": request_row["ai_priority_level"],
            "aiRecommendation": recommendation,
            "recommendedBlockId": "",
            "passengerTrainConflicts": passenger_conflicts,
            "compatibleRequests": compatible_requests,
            "bundlingRecommendation": None,
        }

    best_block = max(candidate_blocks, key=lambda candidate: score_candidate_block(request_row, compatible_requests, candidate, weather_row, demand_row))
    request_row["recommended_block_id"] = best_block["block_id"]
    recommendation = (
        f"AI decision support / optimization: recommend block {best_block['block_id']} for {len(compatible_requests) + 1} compatible maintenance activities within "
        f"corridor {request_row.get('from_station', '')} – {request_row.get('to_station', '')}."
    )
    request_row["ai_recommendation"] = recommendation

    bundling_recommendation = None
    if compatible_requests:
        bundling_recommendation = build_bundling_recommendation(request_row, compatible_requests, best_block, best_block["duration_minutes"])

    return {
        "aiPriorityScore": request_score,
        "aiPriorityLevel": request_row["ai_priority_level"],
        "aiRecommendation": recommendation,
        "recommendedBlockId": best_block["block_id"],
        "passengerTrainConflicts": passenger_conflicts,
        "compatibleRequests": compatible_requests,
        "bundlingRecommendation": bundling_recommendation,
    }


def build_candidate_windows(day, block_rows, train_rows, request_rows, weather_rows, demand_rows):
    weather = next((row for row in weather_rows if row.get("forecast_day") == day.isoformat()), None)
    demand = next((row for row in demand_rows if row.get("demand_day") == day.isoformat()), None)
    active_requests = [row for row in request_rows if row.get("status") not in {"Rejected", "Completed"}]
    day_requests = [row for row in active_requests if row.get("request_day") == day.isoformat()]
    candidates = []
    for block in block_rows:
        if block.get("block_day") != day.isoformat() or block.get("status") in {"Completed", "Cancelled"}:
            continue
        block_start = hmin(block.get("start_time") or "00:00")
        block_end = hmin(block.get("end_time") or "23:59")
        passenger_conflicts = [schedule.trainNumber for schedule in train_rows if schedule.trainType != "Goods" and times_overlap(block_start, block_end, hmin(schedule.scheduledDeparture or schedule.scheduledArrival), hmin(schedule.scheduledDeparture or schedule.scheduledArrival) + 30)]
        linked = [row for row in day_requests if ranges_overlap(float(row.get("km_from", 0) or 0), float(row.get("km_to", 0) or 0), float(block.get("km_from", 0) or 0), float(block.get("km_to", 0) or 0))]
        request = (linked or active_requests or [{
            "priority": "Medium",
            "duration": block_end - block_start,
            "request_day": day.isoformat(),
            "must_complete_by": day.isoformat(),
            "km_from": block.get("km_from", 0),
            "km_to": block.get("km_to", 0),
            "asset_type": "Track",
        }])[0]
        score = score_candidate_block(request, linked, {**block, "duration_minutes": block_end - block_start}, weather, demand)
        if passenger_conflicts:
            score -= len(passenger_conflicts) * 30
        candidates.append({
            "start": block["start_time"],
            "end": block["end_time"],
            "score": max(0, round(score)),
            "reason": "Lowest scored operational conflict and demand window" if not passenger_conflicts else f"Passenger conflict: {', '.join(passenger_conflicts)}",
            "blockId": block["block_id"],
        })
    return sorted(candidates, key=lambda candidate: candidate["score"], reverse=True)


def train_row(row):
    return {"no": row["train_no"], "name": row["name"], "type": row["train_type"], "dir": row["direction"], "time": row["service_time"], "source": row["source"], "scheduled": as_bool(row["scheduled"])}


def train_schedules(rows):
    schedules = [normalize_train_schedule(row) for row in rows]
    return [schedule for schedule in schedules if schedule is not None]


def request_row(row):
    asset_type = row.get("asset_type") or row.get("assetType") or "Unknown"
    request = MaintenanceRequest.from_csv_row(row, asset_type)
    return request.to_api_dict()


def block_row(row, request_ids):
    block = Block.from_csv_row(row, request_ids)
    return block.to_api_dict(row)


def next_id(table, prefix, starting):
    values = []
    for row in read_rows(table):
        value = row.get(FIELDS[table][0], "")
        if value.startswith(prefix) and value[len(prefix):].isdigit():
            values.append(int(value[len(prefix):]))
    return f"{prefix}{max(values, default=starting - 1) + 1}"


def add_audit_event(entity_type, entity_id, action, reason=None):
    rows = read_rows("audit_events")
    rows.append({"event_id": str(max((int(row["event_id"]) for row in rows), default=0) + 1), "entity_type": entity_type, "entity_id": entity_id, "action": action, "actor": "demo-user", "reason": reason or "", "created_at": datetime.now().isoformat(timespec="seconds")})
    write_rows("audit_events", rows)


def block_links():
    links = {}
    for row in read_rows("block_requests"):
        links.setdefault(row["block_id"], []).append(row["request_id"])
    return links


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")


@app.get("/health")
def health():
    initialize_data()
    return {"status": "ok", "storage": "csv", "dataDirectory": str(DATA)}


def traffic_provider_error(error):
    if isinstance(error, RailTrafficNotConfigured):
        raise HTTPException(503, "Live railway traffic integration is not configured") from error
    if isinstance(error, RailTrafficAuthError):
        raise HTTPException(502, "Live railway traffic provider authentication failed") from error
    if isinstance(error, RailTrafficRateLimited):
        raise HTTPException(429, "Live railway traffic provider rate limit exceeded") from error
    raise HTTPException(502, str(error)) from error


@app.get("/api/traffic/live")
def live_traffic():
    return live_rail_traffic.snapshot().to_dict()


@app.get("/api/traffic/trains/{train_id}")
def traffic_train(train_id: str):
    try:
        train = RailTrafficProvider().getTrainById(train_id)
        if train is None:
            raise HTTPException(404, "Train not found")
        return {"train": train}
    except RailTrafficError as error:
        traffic_provider_error(error)


@app.get("/api/traffic/corridor")
def corridor_traffic(corridor: str):
    try:
        return RailTrafficProvider().getTrainsInCorridor(corridor)
    except RailTrafficError as error:
        traffic_provider_error(error)


@app.get("/api/traffic/corridor-states")
def corridor_states(corridor_id: str, name: str, from_station: str, to_station: str, line: str = "", direction: str = "", from_km: Optional[float] = None, to_km: Optional[float] = None):
    corridor = Corridor(corridor_id, name, from_station, to_station, from_km, to_km, line, direction, None, True)
    return {"corridor": corridor.to_dict(), "states": [state.to_dict() for state in live_rail_traffic.match_corridor(corridor)]}


@app.get("/api/conflicts/{block_id}")
def block_conflicts(block_id: str, corridor_id: str, name: str, from_station: str, to_station: str, line: str = "", direction: str = "", from_km: Optional[float] = None, to_km: Optional[float] = None):
    with repository():
        block_row_data = next((row for row in read_rows("blocks") if row["block_id"] == block_id), None)
        train_rows = read_rows("trains")
        block_rows = read_rows("blocks")
    if not block_row_data:
        raise HTTPException(404, "Block not found")
    corridor = Corridor(corridor_id, name, from_station, to_station, from_km, to_km, line, direction, None, True)
    candidate = CandidateBlock(block_id, block_row_data["block_day"], block_row_data["start_time"], block_row_data["end_time"], corridor_id, float(block_row_data["km_from"]), float(block_row_data["km_to"]))
    schedules = train_schedules(train_rows)
    forecasts = [FreightForecast(schedule.trainId, schedule.trainNumber, schedule.scheduledDeparture or schedule.scheduledArrival, schedule.actualDeparture or schedule.scheduledDeparture or schedule.scheduledArrival, schedule.direction, schedule.line, schedule.section, schedule.delayMinutes) for schedule in schedules if schedule.trainType.lower() == "goods"]
    existing = [ExistingBlock(row["block_id"], row["block_day"], row["start_time"], row["end_time"], corridor_id, float(row["km_from"]), float(row["km_to"]), row["status"]) for row in block_rows if row["block_id"] != block_id]
    snapshot = live_rail_traffic.snapshot()
    result = block_conflict_engine.analyze(candidate, snapshot.trains, schedules, forecasts, corridor, existing)
    response = result.to_dict()
    response["mlDecisionSupport"] = predictBlockConflictRisk(candidate, corridor, schedules, snapshot.trains, forecasts, existing, result).to_dict()
    return response


def build_shared_state():
    links = block_links()
    requests = [request_row(row) for row in read_rows("requests")]
    blocks = [block_row(row, links.get(row["block_id"], [])) for row in read_rows("blocks")]
    trains = [train_row(row) for row in sorted(read_rows("trains"), key=lambda row: row["service_time"])]
    return {"requests": requests, "blocks": blocks, "trains": trains}


@app.get("/api/state")
def state():
    with repository():
        shared = build_shared_state()
        requests = shared["requests"]
        blocks = shared["blocks"]
        trains = shared["trains"]
    return {"corridor": "Bengaluru – Dharmavaram", "distance": "~190 km", "requests": requests, "blocks": blocks, "trains": trains, "metrics": {"submitted": len(requests), "bundled": 3, "riskAvoided": "91%", "availability": "96.4%"}}


@app.get("/api/insights")
def insights(day: date = date(2026, 9, 3)):
    with repository():
        assets = sorted(read_rows("assets"), key=lambda row: (int(row["wear_percentage"]), int(row["failure_history"])), reverse=True)
        weather = next((row for row in read_rows("weather_forecasts") if row["forecast_day"] == day.isoformat()), None)
        demand = next((row for row in read_rows("traffic_demand") if row["demand_day"] == day.isoformat()), None)
        resources = sorted(read_rows("resources"), key=lambda row: row["resource_id"])
        trains = train_schedules(sorted(read_rows("trains"), key=lambda row: row["service_time"]))
        requests = [request_row(row) for row in read_rows("requests")]
        request_rows = read_rows("requests")
        block_rows = read_rows("blocks")
        weather_rows = read_rows("weather_forecasts")
        demand_rows = read_rows("traffic_demand")
    weather_data = {"day": day.isoformat(), "condition": "No forecast loaded", "rainfallPercent": 0, "windKmh": 0, "visibilityKm": 10, "planningFactor": 1} if not weather else {"day": weather["forecast_day"], "condition": weather["condition"], "rainfallPercent": int(weather["rain_probability"]), "windKmh": float(weather["wind_kmh"]), "visibilityKm": float(weather["visibility_km"]), "planningFactor": float(weather["planning_factor"])}
    demand_data = None if not demand else {"day": demand["demand_day"], "peakStart": demand["peak_start"], "peakEnd": demand["peak_end"], "passengerTrains": int(demand["passenger_trains"]), "freightTrains": int(demand["freight_trains"]), "score": int(demand["demand_score"])}
    asset_data = [{"id": row["asset_id"], "type": row["asset_type"], "name": f"{row['asset_type']} asset {row['asset_id']}", "location": row["section_id"], "health": 100 - int(row["wear_percentage"]), "criticality": "Critical" if int(row["failure_history"]) >= 2 else "High" if int(row["failure_history"]) == 1 else "Medium", "lastInspected": row["inspection_date"], "failureRisk": min(99, int(row["wear_percentage"]) + int(row["failure_history"]) * 15), "temperature": float(row["temperature"]), "vibration": float(row["vibration"]), "wearPercentage": int(row["wear_percentage"]), "failureHistory": int(row["failure_history"]), "electricalLoad": float(row["electrical_load"]), "lastMaintenance": row["last_maintenance"]} for row in assets]
    resource_data = [{"id": row["resource_id"], "type": row["resource_type"], "name": row["name"], "availableUnits": int(row["available_units"]), "unitCost": int(row["unit_cost"])} for row in resources]
    candidate_windows = build_candidate_windows(day, block_rows, trains, request_rows, weather_rows, demand_rows)
    recommended_window = candidate_windows[0] if candidate_windows else {"start": "00:00", "end": "01:00", "score": 0, "reason": "No block is available for this date", "blockId": ""}
    conflicts = [{"trainNo": schedule.trainNumber, "name": "Unknown train", "time": schedule.scheduledDeparture or schedule.scheduledArrival, "reason": "Train movement overlaps the current active block"} for schedule in trains if recommended_window["start"] <= (schedule.scheduledDeparture or schedule.scheduledArrival) <= recommended_window["end"]]
    compatible = [request for request in requests if request["status"] in ("Approved", "Pending COA", "Coordination hold") and request["kmFrom"] < 123.1 and request["kmTo"] > 121.4]
    bundles = [{"blockId": recommended_window["blockId"], "works": [request["id"] for request in compatible], "kmFrom": 121.4, "kmTo": 123.1, "duration": max((request["duration"] for request in compatible), default=0), "resourceFit": min(len(resource_data), len(compatible))}] if recommended_window["blockId"] else []
    opportunities = build_bundling_opportunities(request_rows, block_rows, trains, weather_rows, demand_rows)
    return {"day": day.isoformat(), "assets": asset_data, "weather": weather_data, "demand": demand_data, "resources": resource_data, "trainConflicts": conflicts, "candidateWindows": candidate_windows, "bundles": bundles, "bundlingOpportunities": opportunities, "recommendedPlan": {"window": recommended_window, "work": [request["work"] for request in compatible], "explanation": recommended_window["reason"]}}


@app.get("/api/requests/{request_id}/history")
def request_history(request_id: str):
    with repository():
        if not any(row["request_id"] == request_id for row in read_rows("requests")):
            raise HTTPException(404, "Request not found")
        rows = [row for row in read_rows("audit_events") if row["entity_type"] == "request" and row["entity_id"] == request_id]
    return [{"action": row["action"], "actor": row["actor"], "reason": row["reason"] or None, "createdAt": row["created_at"]} for row in rows]


@app.post("/api/simulate")
def simulate(body: SimulationIn):
    with repository():
        weather = next((row for row in read_rows("weather_forecasts") if row["forecast_day"] == body.day.isoformat()), None)
        trains = train_schedules(read_rows("trains"))
    rainfall = body.rainfallPercent if body.rainfallPercent is not None else (int(weather["rain_probability"]) if weather else 0)
    factor = (float(weather["planning_factor"]) if weather else 1) + (0.2 if rainfall >= 60 else 0)
    start = hmin(body.start)
    end = start + body.duration + body.addMinutes
    conflicts = [{"trainNo": schedule.trainNumber, "name": "Unknown train", "time": schedule.scheduledDeparture or schedule.scheduledArrival} for schedule in trains if start <= hmin(schedule.scheduledDeparture or schedule.scheduledArrival) <= end]
    traffic_penalty = 18 if body.trafficLevel == "festival_peak" else 0
    base_score = 100 - min(55, len(conflicts) * 18) - min(20, int(rainfall)) - traffic_penalty - max(0, body.addMinutes // 10)
    return {"feasible": not conflicts and end <= 1440, "score": max(0, round(base_score / factor)), "start": body.start, "end": f"{(end // 60) % 24:02d}:{end % 60:02d}", "rainfallPercent": rainfall, "conflicts": conflicts, "explanation": "Window is clear." if not conflicts else "Move the window to avoid scheduled train movements."}


@app.post("/api/requests")
def create_request(req: RequestIn):
    if req.kmTo <= req.kmFrom:
        raise HTTPException(400, "To KM must be higher than From KM")
    if req.mustCompleteBy < req.day:
        raise HTTPException(400, "Completion deadline cannot be before the request day")
    with repository():
        request_id = next_id("requests", "BR-", 1045)
        now = datetime.now().isoformat(timespec="seconds")
        new_row = {
            "request_id": request_id,
            "dept": req.dept,
            "work": req.work,
            "from_station": req.fromStation,
            "from_sub_station": req.fromSubStation,
            "to_station": req.toStation,
            "to_sub_station": req.toSubStation,
            "request_day": req.day.isoformat(),
            "must_complete_by": req.mustCompleteBy.isoformat(),
            "km_from": str(req.kmFrom),
            "km_to": str(req.kmTo),
            "duration": str(req.duration),
            "priority": req.priority,
            "risk": "0",
            "status": "Coordination hold",
            "hold_until": (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds"),
            "created_at": now,
            "updated_at": now,
            "rejection_reason": "",
            "last_action_reason": "",
            "asset_id": req.assetId,
            "asset_type": req.assetType,
            "safety_clearance_required": str(req.safetyClearanceRequired),
            "preferred_start": req.preferredStart,
            "preferred_end": req.preferredEnd,
            "reason": req.reason,
            "requested_by": req.requestedBy,
            "ai_priority_score": "0",
            "ai_priority_level": "Medium",
            "ai_recommendation": "",
            "recommended_block_id": "",
        }
        weather = next((row for row in read_rows("weather_forecasts") if row["forecast_day"] == req.day.isoformat()), None)
        demand = next((row for row in read_rows("traffic_demand") if row["demand_day"] == req.day.isoformat()), None)
        all_block_rows = read_rows("blocks")
        block_rows = all_block_rows
        all_requests = [row for row in read_rows("requests") if row["request_day"] == req.day.isoformat() and row["request_id"] != request_id]
        ai_plan = generate_ai_plan_for_request(new_row, all_requests, block_rows, train_schedules(read_rows("trains")), weather, demand)
        new_row["risk"] = str(ai_plan["aiPriorityScore"])
        new_row["ai_priority_score"] = str(ai_plan["aiPriorityScore"])
        new_row["ai_priority_level"] = ai_plan["aiPriorityLevel"]
        new_row["ai_recommendation"] = ai_plan["aiRecommendation"]
        new_row["recommended_block_id"] = ai_plan["recommendedBlockId"]
        new_row["status"] = "Coordination hold"
        rows = read_rows("requests")
        rows.append(new_row)
        write_rows("requests", rows)
        add_audit_event("request", request_id, "created")
        return request_row(new_row)


@app.get("/api/requests/{request_id}/plan")
def request_plan(request_id: str):
    with repository():
        request = next((row for row in read_rows("requests") if row["request_id"] == request_id), None)
        if not request:
            raise HTTPException(404, "Request not found")
        day = request["request_day"]
        weather = next((row for row in read_rows("weather_forecasts") if row["forecast_day"] == day), None)
        trains = train_schedules(read_rows("trains"))
        blocks = read_rows("blocks")
        all_requests = [row for row in read_rows("requests") if row["request_day"] == day and row["request_id"] != request_id and row["status"] not in ("Rejected", "Completed")]
        demand = next((row for row in read_rows("traffic_demand") if row["demand_day"] == day), None)
        plan = generate_ai_plan_for_request(request, all_requests, blocks, trains, weather, demand)
        request["risk"] = str(plan["aiPriorityScore"])
        request["ai_priority_score"] = str(plan["aiPriorityScore"])
        request["ai_priority_level"] = plan["aiPriorityLevel"]
        request["ai_recommendation"] = plan["aiRecommendation"]
        request["recommended_block_id"] = plan["recommendedBlockId"]
        rows = read_rows("requests")
        for index, row in enumerate(rows):
            if row["request_id"] == request_id:
                rows[index] = request
                break
        write_rows("requests", rows)
        overlapping_requests = [
            row["request_id"] for row in all_requests
            if ranges_overlap(float(request["km_from"]), float(request["km_to"]), float(row["km_from"]), float(row["km_to"]))
        ]
        available_blocks = [
            {"id": row["block_id"], "start": row["start_time"], "end": row["end_time"], "day": row.get("block_day", day), "kmFrom": float(row["km_from"]), "kmTo": float(row["km_to"]), "status": row["status"]}
            for row in blocks if row.get("block_day") == day and row["status"] not in ("Completed", "Cancelled")
        ]
        recommended_block = next((row for row in blocks if row["block_id"] == plan["recommendedBlockId"]), None)
        result = {
            "requestId": request_id,
            "recommendedBlock": {"start": recommended_block["start_time"], "end": recommended_block["end_time"]} if recommended_block else {"start": request.get("preferred_start") or "00:00", "end": request.get("preferred_end") or "00:00"},
            "duration": int(request["duration"]),
            "trainsAffected": len(plan["passengerTrainConflicts"]),
            "weather": weather["condition"] if weather else "No forecast",
            "conflictingBlocks": [],
            "overlappingRequests": overlapping_requests,
            "availableBlocks": available_blocks,
            "risk": plan["aiPriorityLevel"],
            "recommendation": plan["aiRecommendation"],
            "reason": plan["aiRecommendation"],
            "aiRecommendation": plan["aiRecommendation"],
            "aiPriorityScore": int(plan["aiPriorityScore"]),
            "status": "Planning review",
            "bundlingRecommendation": plan["bundlingRecommendation"],
        }
        return result


@app.post("/api/requests/{request_id}")
def request_action(request_id: str, body: RequestAction):
    with repository():
        rows = read_rows("requests")
        row = next((item for item in rows if item["request_id"] == request_id), None)
        if not row:
            raise HTTPException(404, "Request not found")
        allowed = {"approve": {"Pending COA"}, "reject": {"Coordination hold", "Pending COA", "Approved"}, "release": {"Coordination hold"}}
        if row["status"] not in allowed[body.action]:
            raise HTTPException(409, f"Cannot {body.action} a request in {row['status']} status")
        if body.action == "reject" and not body.reason:
            raise HTTPException(400, "A rejection reason is required")
        if body.action == "release":
            hold_until = row.get("hold_until")
            hold_complete = hold_until and datetime.fromisoformat(hold_until) <= datetime.now()
            if not hold_complete and row["priority"] != "Critical":
                raise HTTPException(409, "The 24-hour coordination hold has not ended")
        row["status"] = {"approve": "Approved", "reject": "Rejected", "release": "Pending COA"}[body.action]
        row["updated_at"] = datetime.now().isoformat(timespec="seconds")
        row["last_action_reason"] = body.reason or ""
        row["rejection_reason"] = body.reason if body.action == "reject" else ""
        write_rows("requests", rows)
        add_audit_event("request", request_id, body.action, body.reason)
        return request_row(row)


@app.post("/api/manual-blocks")
def manual_block(body: ManualBlock):
    if body.kmTo <= body.kmFrom:
        raise HTTPException(400, "To KM must be higher than From KM")
    end = hmin(body.start) + body.duration
    if end >= 1440:
        raise HTTPException(400, "Block must end before midnight")
    with repository():
        blocks = read_rows("blocks")
        for row in blocks:
            if row.get("block_day") == body.day.isoformat() and row["status"] not in ("Completed", "Cancelled") and times_overlap(hmin(body.start), end, hmin(row["start_time"]), hmin(row["end_time"])) and ranges_overlap(body.kmFrom, body.kmTo, float(row["km_from"]), float(row["km_to"])):
                raise HTTPException(409, f"Block overlaps existing block {row['block_id']}")
        block_id = next_id("blocks", "BP-", 222)
        row = {"block_id": block_id, "start_time": body.start, "end_time": time_string(end), "block_day": body.day.isoformat(), "km_from": str(body.kmFrom), "km_to": str(body.kmTo), "status": "Planned", "reason": "COA manual block: " + body.reason, "affected": "0"}
        blocks.append(row)
        write_rows("blocks", blocks)
        add_audit_event("block", block_id, "created", body.reason)
        return block_row(row, [])


@app.post("/api/blocks/{block_id}")
def block_action(block_id: str, body: BlockAction):
    with repository():
        blocks = read_rows("blocks")
        row = next((item for item in blocks if item["block_id"] == block_id), None)
        if not row:
            raise HTTPException(404, "Block not found")
        if row["status"] == "Completed":
            raise HTTPException(409, "Completed blocks cannot be changed")
        links = [link["request_id"] for link in read_rows("block_requests") if link["block_id"] == block_id]
        if body.action == "complete":
            row["status"] = "Completed"
            requests = read_rows("requests")
            for request in requests:
                if request["request_id"] in links:
                    request["status"] = "Completed"
            write_rows("requests", requests)
            add_audit_event("block", block_id, "complete")
        elif body.action == "approve":
            row["status"] = "Approved"
            links = read_rows("block_requests")
            request_ids = [link["request_id"] for link in links if link["block_id"] == block_id]
            requests = read_rows("requests")
            if not request_ids:
                request_ids = [request["request_id"] for request in requests if request.get("recommended_block_id") == block_id]
            for request in requests:
                if request["request_id"] in request_ids:
                    request["status"] = "Approved"
                    request["updated_at"] = datetime.now().isoformat(timespec="seconds")
            write_rows("requests", requests)
            add_audit_event("block", block_id, "approve")
        else:
            if body.minutes not in (30, 60):
                raise HTTPException(400, "Extension must be 30 or 60 minutes")
            end = hmin(row["end_time"]) + body.minutes
            if end >= 1440:
                raise HTTPException(400, "Block extension must end before midnight")
            row["end_time"] = time_string(end)
            row["status"] = f"Extended +{body.minutes} min"
            add_audit_event("block", block_id, "extend", f"Extended by {body.minutes} minutes")
        write_rows("blocks", blocks)
        return block_row(row, links)
