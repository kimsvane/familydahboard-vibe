"""Sensor entities for Family Dashboard."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import (
    DOMAIN,
    NEXT_BIRTHDAY,
    NEXT_EVENT,
    FamilyDashboardCoordinator,
    _record_datetime,
    _record_name,
)

PARALLEL_UPDATES = 0

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=NEXT_EVENT,
        translation_key=NEXT_EVENT,
        icon="mdi:calendar-clock",
    ),
    SensorEntityDescription(
        key=NEXT_BIRTHDAY,
        translation_key=NEXT_BIRTHDAY,
        icon="mdi:cake",
    ),
)

_CREDENTIAL_MARKERS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
)


def _public_attributes(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return record attributes without credential-like fields."""
    return {
        key: value
        for key, value in record.items()
        if isinstance(key, str)
        and not any(marker in key.lower() for marker in _CREDENTIAL_MARKERS)
    }


class FamilyDashboardSensor(
    CoordinatorEntity[FamilyDashboardCoordinator], SensorEntity
):
    """Represent the next Family Dashboard event or birthday."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FamilyDashboardCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize a Family Dashboard sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Family Dashboard",
            configuration_url=entry.data[CONF_URL],
        )

    @property
    def _record(self) -> dict[str, Any] | None:
        """Return the selected record."""
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def available(self) -> bool:
        """Return whether a current next item is available."""
        return super().available and self._record is not None

    @property
    def native_value(self) -> str | None:
        """Return the selected item's display name."""
        if self._record is None:
            return None
        return _record_name(self._record, self.entity_description.key)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return useful attributes from the selected item."""
        if self._record is None:
            return {}

        attributes = _public_attributes(self._record)
        time_zone = ZoneInfo(self.coordinator.hass.config.time_zone)
        now = dt_util.now().astimezone(time_zone)
        next_at: datetime | None = _record_datetime(
            self._record, self.entity_description.key, now, time_zone
        )
        if next_at is not None:
            attributes.setdefault("next_at", next_at.isoformat())
            attributes["days_until"] = (next_at.date() - now.date()).days
        return attributes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Family Dashboard sensors."""
    coordinator: FamilyDashboardCoordinator = entry.runtime_data
    async_add_entities(
        FamilyDashboardSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )
