from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from backend.rail_corridor_matching import RailwayCorridorMatchingService
from backend.rail_traffic_models import Corridor, TrainPosition, TrainSchedule, parse_timestamp


@dataclass(frozen=True)
class CandidateBlock:
    blockId: str
    date: str
    startTime: str
    endTime: str
    corridorId: str
    fromKm: Optional[float] = None
    toKm: Optional[float] = None


@dataclass(frozen=True)
class FreightForecast:
    trainId: str
    trainNumber: str
    expectedEntryTime: Optional[str]
    expectedExitTime: Optional[str]
    direction: str = "unknown"
    line: str = ""
    section: str = ""
    delayMinutes: int = 0
    dataSource: str = "freight-forecast"


@dataclass(frozen=True)
class ExistingBlock:
    blockId: str
    date: str
    startTime: str
    endTime: str
    corridorId: str
    fromKm: Optional[float] = None
    toKm: Optional[float] = None
    status: str = "Planned"


@dataclass(frozen=True)
class ConflictExplanation:
    trainId: str
    trainNumber: str
    trainType: str
    severity: str
    explanation: str
    entryTime: Optional[str]
    exitTime: Optional[str]
    overlapMinutes: int
    distanceKm: Optional[float]
    delayMinutes: int
    direction: str
    confidence: str
    dataSource: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BlockConflictAnalysis:
    blockId: str
    passengerConflicts: list[ConflictExplanation]
    freightConflicts: list[ConflictExplanation]
    passengerConflictProbability: float
    freightConflictProbability: float
    nearestTrainTimeToConflict: Optional[str]
    numberOfPassengerConflicts: int
    numberOfFreightConflicts: int
    operationalRiskScore: int
    isFeasible: bool
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["passengerConflicts"] = [item.to_dict() for item in self.passengerConflicts]
        result["freightConflicts"] = [item.to_dict() for item in self.freightConflicts]
        return result


class BlockConflictAnalysisEngine:
    """Deterministic conflict analysis over normalized railway data.

    This is rule-based operational decision support, not an ML model. A train
    occupies a corridor for the interval supplied by live mapping or schedule
    evidence; no geographic precision is inferred from latitude/longitude.
    """

    def __init__(self, corridor_matcher: Optional[RailwayCorridorMatchingService] = None):
        self.corridor_matcher = corridor_matcher or RailwayCorridorMatchingService()

    def analyze(
        self,
        candidate: CandidateBlock,
        live_positions: list[TrainPosition],
        schedules: list[TrainSchedule],
        freight_forecasts: list[FreightForecast],
        corridor: Corridor,
        existing_blocks: list[ExistingBlock],
    ) -> BlockConflictAnalysis:
        passenger_conflicts = self._passenger_conflicts(candidate, live_positions, schedules, corridor)
        freight_conflicts = self._freight_conflicts(candidate, freight_forecasts, corridor)
        existing_overlap = self._existing_block_overlap(candidate, existing_blocks)
        all_conflicts = passenger_conflicts + freight_conflicts
        relevant_passenger = self._relevant_passenger_count(live_positions, schedules, corridor)
        relevant_freight = sum(1 for forecast in freight_forecasts if self._forecast_relevant(forecast, corridor))
        hard_conflict = bool(passenger_conflicts or existing_overlap)
        warning_conflict = bool(freight_conflicts)
        risk = min(100, len(passenger_conflicts) * 45 + len(freight_conflicts) * 18 + (35 if existing_overlap else 0))
        if all_conflicts:
            nearest = min((item.entryTime for item in all_conflicts if item.entryTime), key=self._time_key, default=None)
        else:
            nearest = None
        explanations = [item.explanation for item in all_conflicts]
        if existing_overlap:
            explanations.append(existing_overlap)
        explanation = " ".join(explanations) if explanations else "NO CONFLICT: no meaningful passenger, freight, or possession overlap detected."
        if hard_conflict:
            explanation = "HARD CONFLICT: " + explanation
        elif warning_conflict:
            explanation = "WARNING: " + explanation
        return BlockConflictAnalysis(
            blockId=candidate.blockId,
            passengerConflicts=passenger_conflicts,
            freightConflicts=freight_conflicts,
            passengerConflictProbability=self._probability(len(passenger_conflicts), relevant_passenger),
            freightConflictProbability=self._probability(len(freight_conflicts), relevant_freight),
            nearestTrainTimeToConflict=nearest,
            numberOfPassengerConflicts=len(passenger_conflicts),
            numberOfFreightConflicts=len(freight_conflicts),
            operationalRiskScore=risk,
            isFeasible=not hard_conflict,
            explanation=explanation,
        )

    def _passenger_conflicts(self, candidate, positions, schedules, corridor):
        results = []
        position_by_id = {position.trainId: position for position in positions}
        seen = set()
        for schedule in schedules:
            if schedule.trainType.lower() == "goods" or not self._schedule_relevant(schedule, corridor):
                continue
            train = position_by_id.get(schedule.trainId)
            if train:
                state = self.corridor_matcher.match_train(train, corridor)
                entry, exit_time = self._live_interval(state, schedule, candidate)
                distance = state.distanceKm
                direction = state.direction
                confidence = state.confidence
                delay = train.delayMinutes
                source = train.dataSource
                train_id = train.trainId
                number = train.trainNumber
            else:
                entry, exit_time = self._schedule_interval(schedule)
                distance = None
                direction = self._schedule_direction(schedule, corridor)
                confidence = "medium" if direction != "unknown" else "low"
                delay = schedule.delayMinutes
                source = "timetable"
                train_id = schedule.trainId
                number = schedule.trainNumber
            if train_id in seen:
                continue
            seen.add(train_id)
            overlap = self._overlap_minutes(candidate, entry, exit_time)
            if overlap:
                results.append(self._conflict(train_id, number, "Passenger", "HARD CONFLICT", entry, exit_time, overlap, distance, delay, direction, confidence, source, "Passenger movement overlaps the proposed block; the block is not feasible."))
        return results

    def _freight_conflicts(self, candidate, forecasts, corridor):
        results = []
        seen = set()
        for forecast in forecasts:
            if forecast.trainId in seen or not self._forecast_relevant(forecast, corridor):
                continue
            seen.add(forecast.trainId)
            overlap = self._overlap_minutes(candidate, forecast.expectedEntryTime, forecast.expectedExitTime)
            if overlap:
                results.append(self._conflict(forecast.trainId, forecast.trainNumber, "Goods", "WARNING", forecast.expectedEntryTime, forecast.expectedExitTime, overlap, None, forecast.delayMinutes, self._direction(forecast.direction, corridor), "low", forecast.dataSource, "Freight forecast overlaps the proposed block; coordination is required, but the block may remain feasible."))
        return results

    def _live_interval(self, state, schedule, candidate):
        if state.state == "inside":
            return candidate.startTime, candidate.endTime
        if state.estimatedEntryTime and state.estimatedExitTime:
            return state.estimatedEntryTime, state.estimatedExitTime
        return self._schedule_interval(schedule)

    @staticmethod
    def _schedule_interval(schedule):
        entry = schedule.actualDeparture or schedule.scheduledDeparture or schedule.actualArrival or schedule.scheduledArrival
        if not entry:
            return None, None
        try:
            end = datetime.strptime(entry, "%H:%M") + timedelta(minutes=30)
            return entry, end.strftime("%H:%M")
        except ValueError:
            return entry, None

    @staticmethod
    def _overlap_minutes(candidate, entry, exit_time):
        if not entry or not exit_time:
            return 0
        try:
            start = datetime.strptime(candidate.startTime, "%H:%M")
            end = datetime.strptime(candidate.endTime, "%H:%M")
            train_start = datetime.strptime(entry[-5:], "%H:%M")
            train_end = datetime.strptime(exit_time[-5:], "%H:%M")
        except ValueError:
            return 0
        return max(0, int((min(end, train_end) - max(start, train_start)).total_seconds() / 60))

    @staticmethod
    def _schedule_relevant(schedule, corridor):
        return not schedule.line or not corridor.line or schedule.line == corridor.line or schedule.section == corridor.corridorId or schedule.station in {corridor.fromStation, corridor.toStation}

    @staticmethod
    def _forecast_relevant(forecast, corridor):
        return not forecast.line or not corridor.line or forecast.line == corridor.line or forecast.section == corridor.corridorId

    @staticmethod
    def _schedule_direction(schedule, corridor):
        return BlockConflictAnalysisEngine._direction(schedule.direction, corridor)

    @staticmethod
    def _direction(direction, corridor):
        value = (direction or "").lower()
        if corridor.fromStation.lower() in value and corridor.toStation.lower() in value:
            return "forward"
        if corridor.toStation.lower() in value and corridor.fromStation.lower() in value:
            return "reverse"
        return direction or "unknown"

    @staticmethod
    def _relevant_passenger_count(positions, schedules, corridor):
        return sum(1 for schedule in schedules if schedule.trainType.lower() != "goods" and BlockConflictAnalysisEngine._schedule_relevant(schedule, corridor))

    @staticmethod
    def _probability(conflicts, relevant):
        return round(conflicts / relevant, 3) if relevant else 0.0

    @staticmethod
    def _conflict(train_id, train_number, train_type, severity, entry, exit_time, overlap, distance, delay, direction, confidence, source, explanation):
        return ConflictExplanation(train_id, train_number, train_type, severity, explanation, entry, exit_time, overlap, distance, delay, direction, confidence, source)

    @staticmethod
    def _existing_block_overlap(candidate, existing_blocks):
        for block in existing_blocks:
            if block.status in {"Completed", "Cancelled"} or block.date != candidate.date or block.corridorId != candidate.corridorId:
                continue
            if BlockConflictAnalysisEngine._overlap_minutes(candidate, block.startTime, block.endTime):
                return f"Existing block {block.blockId} overlaps the proposed possession on the same corridor."
        return None

    @staticmethod
    def _time_key(value):
        try:
            return datetime.strptime(value[-5:], "%H:%M")
        except (ValueError, TypeError):
            return datetime.max
