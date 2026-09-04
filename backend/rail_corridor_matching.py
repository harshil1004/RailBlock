import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from backend.rail_traffic_models import Corridor, TrainCorridorState, TrainPosition


@dataclass(frozen=True)
class MappingEvidence:
    state: str
    distance_km: Optional[float] = None
    position_km: Optional[float] = None
    confidence: str = "low"


class CorridorMapping(Protocol):
    def locate(self, train: TrainPosition, corridor: Corridor) -> Optional[MappingEvidence]:
        """Return mapping evidence without fabricating geographic precision."""


class PrototypeCorridorMapping:
    """Replaceable prototype mapping layer.

    It uses explicit section identifiers and station names only. It does not
    calculate distance from latitude/longitude. Pass section ranges or station
    kilometre points when trusted railway GIS data becomes available.
    """

    def __init__(self, section_ranges: Optional[dict[str, tuple[float, float]]] = None, station_km: Optional[dict[str, float]] = None):
        self.section_ranges = {self._key(name): value for name, value in (section_ranges or {}).items()}
        self.station_km = {self._key(name): value for name, value in (station_km or {}).items()}

    def locate(self, train: TrainPosition, corridor: Corridor) -> Optional[MappingEvidence]:
        train_section = self._key(train.section)
        corridor_section = self._key(corridor.corridorId)
        corridor_range = self._range(corridor)
        mapped_range = self.section_ranges.get(train_section)
        if train_section and mapped_range and corridor_range and self._overlaps(mapped_range, corridor_range):
            midpoint = (mapped_range[0] + mapped_range[1]) / 2
            return MappingEvidence("inside", distance_km=0.0, position_km=midpoint, confidence="high")
        if train_section and corridor_section and train_section == corridor_section:
            return MappingEvidence("inside", distance_km=0.0, confidence="medium")
        return None

    @staticmethod
    def _key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    def _range(self, corridor: Corridor) -> Optional[tuple[float, float]]:
        if corridor.fromKm is None or corridor.toKm is None:
            return None
        return corridor.fromKm, corridor.toKm

    @staticmethod
    def _overlaps(first: tuple[float, float], second: tuple[float, float]) -> bool:
        return min(first[1], second[1]) > max(first[0], second[0])


class RailwayCorridorMatchingService:
    def __init__(self, mapping: Optional[CorridorMapping] = None):
        self.mapping = mapping or PrototypeCorridorMapping()

    def match_train(self, train: TrainPosition, corridor: Corridor) -> TrainCorridorState:
        evidence = self.mapping.locate(train, corridor)
        direction = self._relative_direction(train, corridor)
        if evidence:
            state = evidence.state
            distance_km = evidence.distance_km
            confidence = evidence.confidence
        else:
            state, distance_km, confidence = self._station_match(train, corridor, direction)
        entry, exit_time = self._estimate_times(train, corridor, state, distance_km, evidence)
        return TrainCorridorState(
            trainId=train.trainId,
            corridorId=corridor.corridorId,
            state=state,
            distanceKm=distance_km,
            direction=direction,
            estimatedEntryTime=entry,
            estimatedExitTime=exit_time,
            confidence=confidence,
            dataSource=train.dataSource,
            isLive=train.isLive,
        )

    def match_trains(self, trains: list[TrainPosition], corridor: Corridor) -> list[TrainCorridorState]:
        return [self.match_train(train, corridor) for train in trains]

    def match_all(self, trains: list[TrainPosition], corridors: list[Corridor]) -> list[TrainCorridorState]:
        return [self.match_train(train, corridor) for train in trains for corridor in corridors]

    def _station_match(self, train: TrainPosition, corridor: Corridor, direction: str) -> tuple[str, Optional[float], str]:
        current = PrototypeCorridorMapping._key(train.currentStation)
        next_station = PrototypeCorridorMapping._key(train.nextStation)
        from_station = PrototypeCorridorMapping._key(corridor.fromStation)
        to_station = PrototypeCorridorMapping._key(corridor.toStation)
        if current and (current == from_station or current == to_station):
            if next_station in {from_station, to_station} and next_station != current:
                return "inside", None, "medium"
            return "leaving", None, "medium"
        if next_station and next_station in {from_station, to_station}:
            return "approaching", None, "medium"
        if direction != "unknown" and self._line_matches(train, corridor):
            return "outside relevant corridor", None, "low"
        return "outside relevant corridor", None, "low"

    def _relative_direction(self, train: TrainPosition, corridor: Corridor) -> str:
        value = (train.direction or "").lower()
        normalized_value = PrototypeCorridorMapping._key(value)
        from_station = PrototypeCorridorMapping._key(corridor.fromStation)
        to_station = PrototypeCorridorMapping._key(corridor.toStation)
        if "->" in value or "-" in value:
            parts = re.split(r"\s*(?:->|-)\s*", value, maxsplit=1)
            if len(parts) == 2:
                if PrototypeCorridorMapping._key(parts[0]) == from_station and PrototypeCorridorMapping._key(parts[1]) == to_station:
                    return "forward"
                if PrototypeCorridorMapping._key(parts[0]) == to_station and PrototypeCorridorMapping._key(parts[1]) == from_station:
                    return "reverse"
        if normalized_value in {"forward", "up", "east", "updirection"}:
            return "forward"
        if normalized_value in {"reverse", "down", "west", "downdirection"}:
            return "reverse"
        if from_station and to_station:
            if from_station in normalized_value and to_station in normalized_value:
                return "forward"
            if to_station in normalized_value and from_station in normalized_value:
                return "reverse"
        corridor_direction = PrototypeCorridorMapping._key(corridor.direction)
        if corridor_direction and normalized_value == corridor_direction:
            return "forward"
        return "unknown"

    def _line_matches(self, train: TrainPosition, corridor: Corridor) -> bool:
        return not train.line or not corridor.line or PrototypeCorridorMapping._key(train.line) == PrototypeCorridorMapping._key(corridor.line)

    def _estimate_times(self, train: TrainPosition, corridor: Corridor, state: str, distance_km: Optional[float], evidence: Optional[MappingEvidence]) -> tuple[Optional[str], Optional[str]]:
        if distance_km is None or train.speed is None or train.speed <= 0 or corridor.fromKm is None or corridor.toKm is None:
            return None, None
        if evidence and evidence.position_km is not None:
            position_km = evidence.position_km
        else:
            return None, None
        now = datetime.now(timezone.utc)
        corridor_start = min(corridor.fromKm, corridor.toKm)
        corridor_end = max(corridor.fromKm, corridor.toKm)
        entry_minutes = max(0, (corridor_start - position_km) / train.speed * 60)
        exit_minutes = max(0, (corridor_end - position_km) / train.speed * 60)
        entry = now + timedelta(minutes=entry_minutes) if state == "approaching" else None
        exit_time = now + timedelta(minutes=exit_minutes) if state in {"inside", "approaching"} else None
        return self._format(entry), self._format(exit_time)

    @staticmethod
    def _format(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None
