import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from backend.block_conflict_engine import BlockConflictAnalysis, CandidateBlock, FreightForecast
from backend.rail_traffic_models import Corridor, TrainPosition, TrainSchedule, safe_float, safe_int


MODEL_VERSION = "synthetic-prototype-v1"
TRAINING_DATA_LABEL = "SYNTHETIC PROTOTYPE DATA"


@dataclass(frozen=True)
class PredictionResult:
    prediction: Any
    probability: Optional[float]
    score: float
    modelVersion: str
    timestamp: str
    featuresUsed: list[str]
    trainingData: str = TRAINING_DATA_LABEL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MLPredictionService:
    """Decision-support model boundary.

    This fallback is deterministic and intentionally simple because no trained
    XGBoost or LightGBM model is installed. Replace the private scorers with a
    versioned, validated model artifact when production training data exists.
    It never changes deterministic safety or feasibility decisions.
    """

    def predictMaintenanceRisk(self, request: Any, asset: Optional[Any] = None) -> PredictionResult:
        features = {
            "defectSeverity": self._category(request, "defectSeverity", "defect_severity", "priority", default="Medium"),
            "daysOverdue": max(0, self._int(request, "daysOverdue", "days_overdue")),
            "assetCriticality": self._category(request, "assetCriticality", "asset_criticality", default="Medium"),
            "historicalDefectCount": self._int(request, "historicalDefectCount", "historical_defect_count"),
            "historicalFailureCount": self._int(asset or request, "historicalFailureCount", "failure_history"),
            "assetAge": self._int(asset or request, "assetAge", "asset_age"),
            "maintenanceType": self._category(request, "maintenanceType", "activity", "work"),
            "maintenanceComplexity": self._int(request, "maintenanceComplexity", "maintenance_complexity", default=1),
            "trafficExposure": self._category(request, "trafficExposure", "traffic_impact", default="Moderate"),
        }
        severity = {"low": 0.2, "medium": 0.5, "high": 0.78, "critical": 0.95}.get(features["defectSeverity"].lower(), 0.5)
        criticality = {"low": 0.2, "medium": 0.5, "high": 0.78, "critical": 0.95}.get(features["assetCriticality"].lower(), 0.5)
        traffic = {"low": 0.2, "moderate": 0.55, "high": 0.8}.get(features["trafficExposure"].lower(), 0.55)
        score = min(0.99, max(0.01, severity * 0.32 + criticality * 0.22 + min(features["daysOverdue"] / 30, 1) * 0.16 + min(features["historicalFailureCount"] / 3, 1) * 0.12 + min(features["historicalDefectCount"] / 5, 1) * 0.06 + min(features["assetAge"] / 30, 1) * 0.04 + min(features["maintenanceComplexity"] / 5, 1) * 0.04 + traffic * 0.04))
        return self._result("high" if score >= 0.7 else "medium" if score >= 0.4 else "low", score, features)

    def predictExpectedMaintenanceDuration(self, request: Any, asset: Optional[Any] = None) -> PredictionResult:
        features = {
            "department": self._category(request, "department", "dept", default="TMS"),
            "maintenanceActivity": self._category(request, "maintenanceActivity", "activity", "work"),
            "assetType": self._category(request, "assetType", "asset_type", default="Track"),
            "historicalDuration": max(0, self._int(request, "historicalDuration", "historical_duration", "duration")),
            "complexity": max(1, self._int(request, "complexity", "maintenanceComplexity", default=1)),
            "equipmentRequirement": self._category(request, "equipmentRequirement", "equipment_requirement", default="standard"),
            "crewRequirement": max(1, self._int(request, "crewRequirement", "crew_requirement", default=1)),
        }
        base = {"tms": 90, "smms": 60, "tdms": 75}.get(features["department"].lower(), 60)
        asset_factor = {"track": 1.15, "ohe": 1.1, "structure": 1.3, "signal": 0.9}.get(features["assetType"].lower(), 1.0)
        historical = features["historicalDuration"] or base
        equipment_factor = 1.15 if features["equipmentRequirement"].lower() in {"specialized", "heavy"} else 1.0
        predicted_minutes = round(max(30, (historical * 0.55 + base * 0.45) * asset_factor * (1 + min(features["complexity"] - 1, 4) * 0.08) * equipment_factor / min(features["crewRequirement"], 3) ** 0.15))
        score = min(0.99, max(0.01, 0.5 + min(features["complexity"] / 5, 1) * 0.25 + (0.15 if equipment_factor > 1 else 0)))
        return self._result(predicted_minutes, score, features)

    def predictBlockConflictRisk(self, candidate: CandidateBlock, corridor: Corridor, schedules: list[TrainSchedule], live_positions: list[TrainPosition], freight_forecasts: list[FreightForecast], existing_blocks: list[Any], analysis: Optional[BlockConflictAnalysis] = None) -> PredictionResult:
        features = {
            "timeOfDay": candidate.startTime,
            "dayOfWeek": self._day_of_week(candidate.date),
            "corridor": corridor.corridorId,
            "passengerTraffic": sum(1 for schedule in schedules if schedule.trainType.lower() != "goods"),
            "freightForecast": len(freight_forecasts),
            "liveTrainsApproachingCorridor": sum(1 for position in live_positions if position.nextStation in {corridor.fromStation, corridor.toStation}),
            "currentDelay": sum(max(0, position.delayMinutes) for position in live_positions),
            "historicalTraffic": None,
            "existingBlocks": len(existing_blocks),
            "weather": None,
        }
        passenger_count = analysis.numberOfPassengerConflicts if analysis else 0
        freight_count = analysis.numberOfFreightConflicts if analysis else 0
        overlap_score = min(1.0, passenger_count * 0.45 + freight_count * 0.2)
        approach_score = min(1.0, features["liveTrainsApproachingCorridor"] * 0.15 + features["currentDelay"] / 1200)
        existing_score = min(1.0, features["existingBlocks"] * 0.1)
        score = min(0.99, max(0.01, overlap_score * 0.65 + approach_score * 0.2 + existing_score * 0.15))
        return self._result("high" if score >= 0.7 else "medium" if score >= 0.35 else "low", score, features)

    def _result(self, prediction: Any, score: float, features: dict[str, Any]) -> PredictionResult:
        return PredictionResult(prediction, round(score, 3), round(score * 100, 2), MODEL_VERSION, datetime.now(timezone.utc).isoformat(), list(features.keys()))

    @staticmethod
    def _category(value: Any, *names: str, default: str = "unknown") -> str:
        if isinstance(value, dict):
            for name in names:
                if value.get(name) not in (None, ""):
                    return str(value[name])
        else:
            for name in names:
                found = getattr(value, name, None)
                if found not in (None, ""):
                    return str(found)
        return default

    @staticmethod
    def _int(value: Any, *names: str, default: int = 0) -> int:
        if isinstance(value, dict):
            raw = next((value.get(name) for name in names if value.get(name) not in (None, "")), default)
        else:
            raw = next((getattr(value, name, None) for name in names if getattr(value, name, None) not in (None, "")), default)
        return safe_int(raw, default)

    @staticmethod
    def _day_of_week(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%A")
        except ValueError:
            return "unknown"


_prediction_service = MLPredictionService()


def predictMaintenanceRisk(request: Any, asset: Optional[Any] = None) -> PredictionResult:
    return _prediction_service.predictMaintenanceRisk(request, asset)


def predictExpectedMaintenanceDuration(request: Any, asset: Optional[Any] = None) -> PredictionResult:
    return _prediction_service.predictExpectedMaintenanceDuration(request, asset)


def predictBlockConflictRisk(candidate: CandidateBlock, corridor: Corridor, schedules: list[TrainSchedule], live_positions: list[TrainPosition], freight_forecasts: list[FreightForecast], existing_blocks: list[Any], analysis: Optional[BlockConflictAnalysis] = None) -> PredictionResult:
    return _prediction_service.predictBlockConflictRisk(candidate, corridor, schedules, live_positions, freight_forecasts, existing_blocks, analysis)
