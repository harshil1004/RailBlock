import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from backend.rail_traffic_models import TrainPosition, data_age_seconds
from backend.rail_traffic_provider import RailTrafficError, RailTrafficProvider
from backend.rail_corridor_matching import RailwayCorridorMatchingService


@dataclass(frozen=True)
class LiveTrafficSnapshot:
    trains: list[TrainPosition]
    lastSuccessfulUpdate: Optional[str]
    numberOfTrainsReceived: int
    apiLatencyMs: Optional[float]
    apiError: Optional[str]
    stale: bool
    loading: bool
    status: str
    dataSource: str
    responseTimestamp: Optional[str]
    dataAgeSeconds: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["trains"] = [train.to_dict() for train in self.trains]
        return result


class LiveRailTrafficService:
    """Shared, server-side live traffic cache and refresh lifecycle."""

    def __init__(
        self,
        provider: Optional[RailTrafficProvider] = None,
        refresh_interval_seconds: Optional[float] = None,
        freshness_threshold_seconds: Optional[int] = None,
    ):
        self.provider = provider or RailTrafficProvider()
        self.corridor_matching = RailwayCorridorMatchingService()
        self.refresh_interval_seconds = max(1.0, refresh_interval_seconds if refresh_interval_seconds is not None else float(os.getenv("RAIL_TRAFFIC_REFRESH_INTERVAL_SECONDS", "30")))
        self.freshness_threshold_seconds = max(0, freshness_threshold_seconds if freshness_threshold_seconds is not None else int(os.getenv("RAIL_TRAFFIC_STALE_AFTER_SECONDS", "120")))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._trains: list[TrainPosition] = []
        self._last_successful_update: Optional[str] = None
        self._response_timestamp: Optional[str] = None
        self._api_latency_ms: Optional[float] = None
        self._api_error: Optional[str] = None
        self._loading = False
        self._stale = True

    def start(self, refresh_immediately: bool = True) -> None:
        with self._lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
            self._stop_event.clear()
            self._refresh_thread = threading.Thread(target=self._refresh_loop, name="live-rail-traffic", daemon=True)
            self._refresh_thread.start()
        if refresh_immediately:
            self.refresh()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._refresh_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(self.refresh_interval_seconds, 2.0))
        with self._lock:
            self._refresh_thread = None

    def refresh(self) -> LiveTrafficSnapshot:
        with self._lock:
            self._loading = True
        started = time.monotonic()
        try:
            feed = self.provider.getLiveTrains()
            trains = self._deduplicate(feed.get("trains", []))
            if not trains and feed.get("status") != "empty":
                raise RailTrafficError("Rail traffic provider returned no valid trains")
            now = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._trains = trains
                self._last_successful_update = now
                self._response_timestamp = feed.get("lastUpdated") or now
                self._api_latency_ms = round((time.monotonic() - started) * 1000, 2)
                self._api_error = None
                self._loading = False
                self._stale = self._is_stale(self._response_timestamp, now)
        except RailTrafficError as error:
            with self._lock:
                self._api_latency_ms = round((time.monotonic() - started) * 1000, 2)
                self._api_error = str(error)
                self._loading = False
                self._stale = self._cache_is_stale()
        except Exception:
            with self._lock:
                self._api_latency_ms = round((time.monotonic() - started) * 1000, 2)
                self._api_error = "Unexpected live traffic service error"
                self._loading = False
                self._stale = self._cache_is_stale()
        return self.snapshot()

    def snapshot(self) -> LiveTrafficSnapshot:
        with self._lock:
            stale = self._cache_is_stale()
            status = "loading" if self._loading else "error" if self._api_error and not self._trains else "empty" if not self._trains else "stale" if stale else "live"
            return LiveTrafficSnapshot(
                trains=list(self._trains),
                lastSuccessfulUpdate=self._last_successful_update,
                numberOfTrainsReceived=len(self._trains),
                apiLatencyMs=self._api_latency_ms,
                apiError=self._api_error,
                stale=stale,
                loading=self._loading,
                status=status,
                dataSource="live-api",
                responseTimestamp=self._response_timestamp,
                dataAgeSeconds=data_age_seconds(self._response_timestamp),
            )

    def match_corridor(self, corridor):
        """Match the current shared cache without making a provider request."""
        with self._lock:
            trains = list(self._trains)
        return self.corridor_matching.match_trains(trains, corridor)

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            self.refresh()
            self._stop_event.wait(self.refresh_interval_seconds)

    @staticmethod
    def _deduplicate(trains: list[Any]) -> list[TrainPosition]:
        unique: dict[str, TrainPosition] = {}
        for train in trains:
            if not isinstance(train, TrainPosition) or not train.trainId:
                continue
            unique.setdefault(train.trainId, train)
        return list(unique.values())

    def _cache_is_stale(self) -> bool:
        if not self._response_timestamp:
            return True
        return self._is_stale(self._response_timestamp, datetime.now(timezone.utc).isoformat())

    def _is_stale(self, timestamp: Optional[str], now: str) -> bool:
        if not timestamp:
            return True
        age = data_age_seconds(timestamp, datetime.fromisoformat(now))
        return age is None or age > self.freshness_threshold_seconds
