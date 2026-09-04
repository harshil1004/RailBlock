import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote

import httpx

from backend.rail_traffic_models import (
    Corridor,
    TrainPosition,
    TrainSchedule,
    normalize_corridor,
    normalize_train_position,
    normalize_train_schedule,
)


class RailTrafficError(Exception):
    """Base error for the server-side railway traffic integration."""


class RailTrafficNotConfigured(RailTrafficError):
    pass


class RailTrafficProviderError(RailTrafficError):
    pass


class RailTrafficRateLimited(RailTrafficProviderError):
    pass


class RailTrafficAuthError(RailTrafficProviderError):
    pass


@dataclass
class TrafficFeed:
    trains: list[TrainPosition]
    fetched_at: str
    last_updated: Optional[str]
    stale: bool
    status: str


class RailTrafficProvider:
    """Server-side adapter for a live train feed.

    The provider-specific payload is converted to the application's train shape
    before it leaves this module. Credentials are never included in responses.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        stale_after_seconds: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = (base_url or os.getenv("RAIL_TRAFFIC_API_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("RAIL_TRAFFIC_API_KEY")
        self.timeout_seconds = timeout_seconds or float(os.getenv("RAIL_TRAFFIC_API_TIMEOUT_SECONDS", "8"))
        self.max_retries = max(0, max_retries if max_retries is not None else int(os.getenv("RAIL_TRAFFIC_API_MAX_RETRIES", "2")))
        self.stale_after_seconds = max(0, stale_after_seconds if stale_after_seconds is not None else int(os.getenv("RAIL_TRAFFIC_STALE_AFTER_SECONDS", "120")))
        self.client = client or httpx.Client(timeout=self.timeout_seconds)

    def getLiveTrains(self) -> dict[str, Any]:
        return self._get_feed("/trains")

    def getTrainById(self, trainId: str) -> Optional[TrainPosition]:
        feed = self._get_feed(f"/trains/{quote(trainId, safe='')}")
        return feed["trains"][0] if feed["trains"] else None

    def getTrainsInCorridor(self, corridor: str) -> dict[str, Any]:
        return self._get_feed("/trains", {"corridor": corridor})

    def getSchedules(self, trainId: Optional[str] = None) -> list[TrainSchedule]:
        params = {"trainId": trainId} if trainId else None
        payload = self._request_json("/schedules", params)
        schedules = self._extract_collection(payload, "schedules")
        return [schedule for schedule in (normalize_train_schedule(item) for item in schedules) if schedule is not None]

    def getCorridors(self) -> list[Corridor]:
        payload = self._request_json("/corridors", None)
        corridors = self._extract_collection(payload, "corridors")
        return [corridor for corridor in (normalize_corridor(item) for item in corridors) if corridor is not None]

    def _get_feed(self, path: str, params: Optional[dict[str, str]] = None) -> dict[str, Any]:
        payload = self._request_json(path, params)
        fetched_at = datetime.now(timezone.utc).isoformat()
        raw_trains, last_updated = self._extract_trains(payload)
        stale = self._is_stale(last_updated, fetched_at)
        trains = [normalize_train_position(train, fetched_at, stale) for train in raw_trains]
        trains = [train for train in trains if train is not None]
        return {
            "trains": trains,
            "fetchedAt": fetched_at,
            "lastUpdated": last_updated,
            "stale": stale,
            "status": "empty" if not trains else "stale" if stale else "live",
        }

    def _request_json(self, path: str, params: Optional[dict[str, str]]) -> Any:
        if not self.base_url or not self.api_key:
            raise RailTrafficNotConfigured("RAIL_TRAFFIC_API_URL and RAIL_TRAFFIC_API_KEY are required")
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as error:
                if attempt + 1 == attempts:
                    raise RailTrafficProviderError("Rail traffic provider timed out") from error
                self._backoff(attempt)
                continue
            except httpx.HTTPError as error:
                if attempt + 1 == attempts:
                    raise RailTrafficProviderError("Rail traffic provider is unavailable") from error
                self._backoff(attempt)
                continue

            if response.status_code == 429:
                if attempt + 1 == attempts:
                    raise RailTrafficRateLimited("Rail traffic provider rate limit exceeded")
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code in {408, 425} or response.status_code >= 500:
                if attempt + 1 == attempts:
                    raise RailTrafficProviderError(f"Rail traffic provider returned HTTP {response.status_code}")
                self._backoff(attempt)
                continue
            if response.status_code in {401, 403}:
                raise RailTrafficAuthError("Rail traffic provider rejected server credentials")
            if response.status_code == 404:
                raise RailTrafficProviderError("Rail traffic endpoint was not found")
            if response.status_code >= 400:
                raise RailTrafficProviderError(f"Rail traffic provider returned HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as error:
                raise RailTrafficProviderError("Rail traffic provider returned invalid JSON") from error
        raise RailTrafficProviderError("Rail traffic provider request failed")

    def _backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        delay = self._retry_after_seconds(retry_after)
        if delay is None:
            delay = min(2 ** attempt, 4)
        time.sleep(delay)

    @staticmethod
    def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return min(max(float(value), 0), 10)
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                return min(max((target - datetime.now(target.tzinfo)).total_seconds(), 0), 10)
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _extract_trains(payload: Any) -> tuple[list[Any], Optional[str]]:
        if isinstance(payload, list):
            return payload, None
        if not isinstance(payload, dict):
            raise RailTrafficProviderError("Rail traffic provider returned an unexpected payload")
        trains = payload.get("trains") or payload.get("data") or payload.get("results") or []
        if isinstance(trains, dict):
            trains = trains.get("trains") or trains.get("items") or []
        if not isinstance(trains, list):
            raise RailTrafficProviderError("Rail traffic provider returned an invalid train collection")
        return trains, payload.get("lastUpdated") or payload.get("updatedAt") or payload.get("timestamp")

    @staticmethod
    def _extract_collection(payload: Any, key: str) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise RailTrafficProviderError("Rail traffic provider returned an unexpected payload")
        collection = payload.get(key) or payload.get("data") or payload.get("results") or []
        if isinstance(collection, dict):
            collection = collection.get(key) or collection.get("items") or []
        if not isinstance(collection, list):
            raise RailTrafficProviderError(f"Rail traffic provider returned an invalid {key} collection")
        return collection

    def _is_stale(self, last_updated: Optional[str], fetched_at: str) -> bool:
        if not last_updated:
            return False
        try:
            parsed = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            fetched = datetime.fromisoformat(fetched_at)
            return (fetched - parsed).total_seconds() > self.stale_after_seconds
        except ValueError:
            return True
