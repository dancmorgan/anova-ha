"""Patches a crash in the anova-wifi library's A3 EVENT_APC_STATE parsing.

Home Assistant's built-in Anova integration pins anova-wifi==1.0.1. That
version's build_a3_payload() has two bugs that are fatal for an A3/A2
cooker's very first (or an early) websocket message:

1. When the cooker is idle, the device sends `currentJob.jobStage: null`.
   `AnovaA3State(None)` raises ValueError (no `_missing_` fallback), which
   happens on literally the first EVENT_APC_STATE message an idle device
   ever sends - so the coordinator's first update never arrives and no
   entities are ever created.

2. After the initial full state snapshot, the device sends partial delta
   updates that omit unchanged top-level keys (e.g. `timerInSeconds`,
   `currentJob`). build_a3_payload() indexes those keys directly
   (`apc_response["timerInSeconds"]`), so the first delta update raises
   KeyError.

Either exception propagates out of AnovaWebsocketHandler.on_message(),
which is called from an unguarded `async for msg in self.ws:` loop in
message_listener() - so it permanently kills that background task. No
error surfaces in the `anova` or `anova_wifi` loggers because it's an
orphaned asyncio task exception.

This component monkeypatches AnovaWebsocketHandler.on_message at import
time to: treat a missing/unrecognized job stage as "no active job", merge
delta updates onto a cached last-known-full state per device before
parsing, and catch parsing errors per-message (log + skip) instead of
letting them kill the listener. It changes no HA-side integration code -
just the library call it makes.

See: https://github.com/home-assistant/core/issues/118911
     https://github.com/Lash-L/anova_wifi
"""

from __future__ import annotations

import logging
from typing import Any

from anova_wifi.web_socket_containers import (
    AnovaA3State,
    AnovaCommand,
    AnovaState,
    APCUpdate,
    APCUpdateBinary,
    APCUpdateSensor,
    APCWifiDevice,
    build_a6_a7_payload,
    build_wifi_cooker_state_body,
)
from anova_wifi.websocket_handler import AnovaWebsocketHandler
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _build_a3_payload_safe(apc_response: dict[str, Any]) -> APCUpdate:
    """Reimplementation of anova_wifi's build_a3_payload that tolerates
    a null job stage and missing keys, instead of raising."""
    firmware_version = apc_response.get("firmwareVersion")
    is_cooking = bool(apc_response.get("isCooking", False))
    current_temperature = apc_response.get("currentTemperature")
    target_temperature = apc_response.get("targetTemperature")
    timer_in_seconds = apc_response.get("timerInSeconds")
    current_job = apc_response.get("currentJob")
    job_stage = current_job.get("jobStage") if current_job else None

    if job_stage is None:
        status = AnovaA3State.no_state
    else:
        try:
            status = AnovaA3State(job_stage)
        except ValueError:
            _LOGGER.debug("Unrecognized Anova A3 jobStage %r", job_stage)
            status = AnovaA3State.no_state

    sensors = APCUpdateSensor(
        a3_state=status.name,
        target_temperature=(
            float(target_temperature) if target_temperature is not None else None
        ),
        cook_time_remaining=(
            int(timer_in_seconds) if timer_in_seconds is not None else None
        ),
        firmware_version=firmware_version,
        water_temperature=(
            float(current_temperature) if current_temperature is not None else None
        ),
    )
    binary_sensors = APCUpdateBinary(
        cooking=is_cooking,
        preheating=bool(status == AnovaState.preheating),
        maintaining=bool(
            status == AnovaState.maintaining or status == AnovaState.timer_expired
        ),
    )
    return APCUpdate(binary_sensors, sensors)


def _patched_on_message(self: AnovaWebsocketHandler, message: dict[str, Any]) -> None:
    _LOGGER.debug("Found message %s", message)
    if message["command"] == AnovaCommand.EVENT_APC_WIFI_LIST:
        payload = message["payload"]
        for device in payload:
            if device["cookerId"] not in self.devices:
                self.devices[device["cookerId"]] = APCWifiDevice(
                    cooker_id=device["cookerId"],
                    type=device["type"],
                    paired_at=device["pairedAt"],
                    name=device["name"],
                )
    elif message["command"] == AnovaCommand.EVENT_APC_STATE:
        cooker_id = message["payload"]["cookerId"]
        device = self.devices.get(cooker_id)
        if device is None:
            return
        update_listener = device.update_listener
        if update_listener is None:
            return
        state = message["payload"]["state"]
        try:
            if "job" in state:
                update = build_wifi_cooker_state_body(state).to_apc_update()
            elif message["payload"]["type"] == "a3":
                cache: dict[str, dict[str, Any]] = getattr(
                    self, "_a3_state_cache", None
                ) or {}
                self._a3_state_cache = cache
                merged = cache.setdefault(cooker_id, {})
                merged.update(state)
                update = _build_a3_payload_safe(merged)
            elif message["payload"]["type"] in {"a6", "a7"}:
                update = build_a6_a7_payload(state)
            else:
                return
        except (KeyError, ValueError, TypeError) as ex:
            _LOGGER.warning(
                "anova_a3_fix: skipping malformed EVENT_APC_STATE for %s: %s",
                cooker_id,
                ex,
            )
            return
        update_listener(update)


AnovaWebsocketHandler.on_message = _patched_on_message
_LOGGER.warning(
    "anova_a3_fix: patched AnovaWebsocketHandler.on_message to handle "
    "A3 delta updates and null job stages"
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The patch above is applied at import time, which already happened by
    # the time this runs. Nothing left to do per-entry.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The monkeypatch can't be cleanly reverted; removing the entry just
    # stops it from being re-applied on the next restart.
    return True
