from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional


_MISSING = object()


def safe_field(data: Any, *names: str, default: Any = None) -> Any:
    """Read the first present value from a provider object or mapping."""
    if not isinstance(data, dict):
        return default
    for name in names:
        value = data.get(name, _MISSING)
        if value is not _MISSING and value is not None and value != "":
            return value
    return default


def safe_nested_field(data: Any, *paths: tuple[str, ...], default: Any = None) -> Any:
    for path in paths:
        current = data
        for name in path:
            current = safe_field(current, name, default=_MISSING)
            if current is _MISSING:
                break
        else:
            if current is not None and current is not _MISSING and current != "":
                return current
    return default


def safe_string(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return default if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return default if value is None or value == "" else int(float(value))
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "live", "active"}
    return bool(value)


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def data_age_seconds(timestamp: Any, now: Optional[datetime] = None) -> Optional[int]:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, int((current - parsed).total_seconds()))


@dataclass(frozen=True)
class TrainPosition:
    trainId: str
    trainNumber: str
    trainName: str
    trainType: str
    latitude: Optional[float]
    longitude: Optional[float]
    speed: Optional[float]
    direction: str
    currentStation: str
    nextStation: str
    scheduledNextStationTime: Optional[str]
    estimatedNextStationTime: Optional[str]
    delayMinutes: int
    line: str
    section: str
    timestamp: Optional[str]
    dataSource: str
    isLive: bool
    dataAgeSeconds: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrainSchedule:
    trainId: str
    trainNumber: str
    trainType: str
    date: Optional[str]
    station: str
    scheduledArrival: Optional[str]
    scheduledDeparture: Optional[str]
    actualArrival: Optional[str]
    actualDeparture: Optional[str]
    delayMinutes: int
    line: str
    section: str
    direction: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Corridor:
    corridorId: str
    name: str
    fromStation: str
    toStation: str
    fromKm: Optional[float]
    toKm: Optional[float]
    line: str
    direction: str
    geometry: Any
    isAvailable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrainCorridorState:
    trainId: str
    corridorId: str
    state: str
    distanceKm: Optional[float]
    direction: str
    estimatedEntryTime: Optional[str]
    estimatedExitTime: Optional[str]
    confidence: str
    dataSource: str
    isLive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_train_position(raw: Any, fetched_at: Optional[str] = None, stale: bool = False) -> Optional[TrainPosition]:
    if not isinstance(raw, dict):
        return None
    nested_position = safe_field(raw, "position", "location", default={})
    train_id = safe_string(safe_field(raw, "trainId", "train_id", "id", "trainNumber", "train_no"))
    train_number = safe_string(safe_field(raw, "trainNumber", "train_no", "trainNo", "number", "no"))
    if not train_id:
        train_id = train_number
    if not train_id and not train_number:
        return None
    timestamp = safe_field(raw, "timestamp", "updatedAt", "lastUpdated", "observedAt", default=fetched_at)
    age = data_age_seconds(timestamp)
    return TrainPosition(
        trainId=train_id,
        trainNumber=train_number or train_id,
        trainName=safe_string(safe_field(raw, "trainName", "train_name", "name"), "Unknown train"),
        trainType=safe_string(safe_field(raw, "trainType", "train_type", "type"), "Passenger"),
        latitude=safe_float(safe_nested_field(raw, ("latitude",), ("lat",), ("position", "latitude"), ("location", "lat"), default=None)),
        longitude=safe_float(safe_nested_field(raw, ("longitude",), ("lon",), ("lng",), ("position", "longitude"), ("location", "lon"), default=None)),
        speed=safe_float(safe_field(raw, "speed", "speedKph", "speed_kph", "velocity")),
        direction=safe_string(safe_field(raw, "direction", "dir", "heading")),
        currentStation=safe_string(safe_field(raw, "currentStation", "current_station", "station")),
        nextStation=safe_string(safe_field(raw, "nextStation", "next_station")),
        scheduledNextStationTime=safe_field(raw, "scheduledNextStationTime", "scheduled_next_station_time", "scheduledTime", "serviceTime", "service_time"),
        estimatedNextStationTime=safe_field(raw, "estimatedNextStationTime", "estimated_next_station_time", "estimatedTime"),
        delayMinutes=safe_int(safe_field(raw, "delayMinutes", "delay_minutes", "delay")),
        line=safe_string(safe_field(raw, "line", "lineCode")),
        section=safe_string(safe_field(raw, "section", "sectionId", "section_id")),
        timestamp=safe_string(timestamp) or None,
        dataSource=safe_string(safe_field(raw, "dataSource", "data_source", "source"), "live-api"),
        isLive=safe_bool(safe_field(raw, "isLive", "is_live", "live"), default=bool(timestamp) and not stale),
        dataAgeSeconds=age,
    )


def normalize_train_schedule(raw: Any) -> Optional[TrainSchedule]:
    if not isinstance(raw, dict):
        return None
    train_id = safe_string(safe_field(raw, "trainId", "train_id", "id", "trainNumber", "train_no"))
    train_number = safe_string(safe_field(raw, "trainNumber", "train_no", "trainNo", "number", "no"))
    station = safe_string(safe_field(raw, "station", "stationName", "station_name", "stop"))
    if not train_id and not train_number:
        return None
    return TrainSchedule(
        trainId=train_id or train_number,
        trainNumber=train_number or train_id,
        trainType=safe_string(safe_field(raw, "trainType", "train_type", "type"), "Passenger"),
        date=safe_field(raw, "date", "serviceDate", "service_date"),
        station=station,
        scheduledArrival=safe_field(raw, "scheduledArrival", "scheduled_arrival", "serviceTime", "service_time"),
        scheduledDeparture=safe_field(raw, "scheduledDeparture", "scheduled_departure", "serviceTime", "service_time"),
        actualArrival=safe_field(raw, "actualArrival", "actual_arrival"),
        actualDeparture=safe_field(raw, "actualDeparture", "actual_departure"),
        delayMinutes=safe_int(safe_field(raw, "delayMinutes", "delay_minutes", "delay")),
        line=safe_string(safe_field(raw, "line", "lineCode")),
        section=safe_string(safe_field(raw, "section", "sectionId", "section_id")),
        direction=safe_string(safe_field(raw, "direction", "dir")),
    )


def normalize_corridor(raw: Any) -> Optional[Corridor]:
    if not isinstance(raw, dict):
        return None
    corridor_id = safe_string(safe_field(raw, "corridorId", "corridor_id", "id"))
    if not corridor_id and not safe_field(raw, "name", "corridorName"):
        return None
    return Corridor(
        corridorId=corridor_id,
        name=safe_string(safe_field(raw, "name", "corridorName"), "Unnamed corridor"),
        fromStation=safe_string(safe_field(raw, "fromStation", "from_station", "origin")),
        toStation=safe_string(safe_field(raw, "toStation", "to_station", "destination")),
        fromKm=safe_float(safe_field(raw, "fromKm", "from_km")),
        toKm=safe_float(safe_field(raw, "toKm", "to_km")),
        line=safe_string(safe_field(raw, "line", "lineCode")),
        direction=safe_string(safe_field(raw, "direction", "dir")),
        geometry=safe_field(raw, "geometry", "shape", default=None),
        isAvailable=safe_bool(safe_field(raw, "isAvailable", "is_available", "available"), default=True),
    )
