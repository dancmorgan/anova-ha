# Anova A3 WiFi bug fix — handoff brief for a PR to Lash-L/anova_wifi

Paste this whole file to Claude Code at the start of the new session (in the
forked `anova_wifi` workspace) as the project brief. It contains everything
already confirmed so it doesn't need to be re-investigated.

## Goal

Fix two real, confirmed bugs in `Lash-L/anova_wifi`'s A3/A2 websocket state
parsing and submit a PR upstream. A working local reference fix already
exists (see below) — the job here is to port its *behavior* into a proper
source-level patch of the library, with tests, and open the PR.

## Background — already ruled out, do not re-investigate

The originally suspected bug (a top-level `job` key causing `KeyError`,
described in `home-assistant/core` issue
[#118911](https://github.com/home-assistant/core/issues/118911)) does **not**
apply to current payloads. Real A3 devices never send a top-level `job` key —
only a nested `currentJob` under `state`. That code path is dead for A3s.
Don't chase it.

## Confirmed bugs (found via live debug logging against a real A3)

Environment: Anova Precision Cooker A3, firmware `ver 4.2.7`,
`anova_wifi==1.0.1` (the version pinned by Home Assistant core's `anova`
integration), Home Assistant on Raspberry Pi 5.

Both bugs live in `build_a3_payload()` in `web_socket_containers.py`.

### Bug 1 — null `jobStage` crashes on the cooker's very first message

When idle, the A3's first `EVENT_APC_STATE` message includes:

```json
"currentJob": {"jobType": null, "jobStage": null, "targetTemperature": 60, "timerLength": 0, "tempUnit": "c", "thresholdTemperature": null, "thresholdTemperatureUnit": "c"}
```

`build_a3_payload()` passes `jobStage` (`None`) straight into
`AnovaA3State(...)`, which raises `ValueError` — there's no `_missing_`
fallback for that enum. This happens on literally the first message an idle
A3 ever sends, so the HA coordinator's first update never completes and no
entities are ever created.

### Bug 2 — delta updates omit unchanged keys, causing KeyError

After the first full snapshot, later `EVENT_APC_STATE` messages in the same
session can omit top-level keys that haven't changed (e.g. `currentJobID`,
`currentJob`, `timerInSeconds` are present in message 1 but absent from
message 2, sent ~20ms later — see real payloads below).
`build_a3_payload()` indexes some of these directly
(`apc_response["someKey"]`), which raises `KeyError` on any message that
omits them.

### Why this is invisible in normal operation

Both exceptions happen inside `AnovaWebsocketHandler.on_message()`, called
from an unguarded `async for msg in self.ws:` loop in `message_listener()`.
An uncaught exception there silently kills that background task — nothing
logs an error in the `anova` or `anova_wifi` loggers, because it's an
orphaned asyncio task exception. User-visible symptom: HA login succeeds,
websocket connects, but no devices/entities ever appear — looks identical to
"no devices found" during config flow, which is why this was hard to
diagnose without debug logging.

## Real evidence — actual captured payloads from a live A3

Use these as the basis for test fixtures (cookerId can be anonymized).

Full snapshot (first `EVENT_APC_STATE` message):

```python
{'command': 'EVENT_APC_STATE', 'payload': {'cookerId': 'anova f56-a84574f308e', 'type': 'a3', 'state': {'firmwareVersion': 'ver 4.2.7', 'isCooking': False, 'currentTemperature': 19.1, 'targetTemperature': 60, 'timerInSeconds': 0, 'unit': 'c', 'isTimerRunning': False, 'isSpeakerOn': True, 'isAlarmActive': False, 'currentJobID': '', 'currentJob': {'jobType': None, 'jobStage': None, 'targetTemperature': 60, 'timerLength': 0, 'tempUnit': 'c', 'thresholdTemperature': None, 'thresholdTemperatureUnit': 'c'}, 'isKeepingWarm': False, 'isCheckingTemperatureForIceBath': False, 'isMonitoringIcebath': False, 'isConnected': True}}}
```

Delta update (~20ms later, same session, missing several top-level keys):

```python
{'command': 'EVENT_APC_STATE', 'payload': {'cookerId': 'anova f56-a84574f308e', 'type': 'a3', 'state': {'firmwareVersion': 'ver 4.2.7', 'isCooking': False, 'currentTemperature': 19.1, 'targetTemperature': 60, 'unit': 'c', 'isTimerRunning': False, 'isSpeakerOn': True, 'isAlarmActive': False, 'isKeepingWarm': False, 'isCheckingTemperatureForIceBath': False, 'isMonitoringIcebath': False, 'isConnected': True}}}
```

Note what's missing from the second message versus the first:
`currentJobID`, `currentJob`, `timerInSeconds`.

## Reference implementation — already working, use as the behavioral spec

A local Home Assistant custom_component (repo `dancmorgan/anova-ha`,
`custom_components/anova_a3_fix/__init__.py`) already implements and
validates a fix via a runtime monkeypatch of
`AnovaWebsocketHandler.on_message`, confirmed working against the real
cooker above (entities now populate correctly in HA). Port its *behavior*
into a real source-level fix in `anova_wifi` — don't just copy the
monkeypatch mechanism (that's a HA-side workaround, not appropriate for the
library itself).

The working reimplementation of `build_a3_payload`, for reference:

```python
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
```

The HA-side patch also caches and merges delta updates onto a per-device
last-known-full-state dict before calling this function, since a delta
update alone doesn't carry enough information (e.g. `currentJob` might be
entirely absent). That merge-caching happened at the websocket handler
layer in the HA workaround, purely because that's the only layer with
patchable access to per-device state across messages.

Key behaviors to preserve in the real fix:

- Treat a `None`, missing, or unrecognized `jobStage` as "no active job"
  (`AnovaA3State.no_state`) instead of raising `ValueError`.
- Use `.get()` with sensible fallbacks for all top-level keys instead of
  direct indexing, so a delta update that omits unchanged keys doesn't raise
  `KeyError`.
- Decide where delta-update merging belongs in the *actual* library
  architecture — this needs fresh eyes on the current `anova_wifi` source,
  which may have moved on since `1.0.1`. Options: make `build_a3_payload`
  itself tolerant of partial input (return `None`/unknown for omitted
  fields), or maintain last-known-state per device inside
  `AnovaWebsocketHandler` itself (stateful, mirrors the HA workaround, but
  is more invasive to the library's design — check how state is currently
  tracked there before choosing this).

## Plan

1. Clone the fork, read the current `web_socket_containers.py` and
   `websocket_handler.py` to confirm these bugs still exist as described —
   the library has had further releases since `1.0.1`, so verify before
   patching rather than assuming the code hasn't moved.
2. Implement the fix directly in the library source, matching the required
   behaviors above (this is a rewrite in the target codebase's own style,
   not a copy-paste of the reference code, which was written for a
   monkeypatch context).
3. Add test fixtures using the two real payloads captured above.
4. Open a PR to `Lash-L/anova_wifi`. Reference
   `home-assistant/core#118911` in the description and explain both bugs
   with the real payload evidence (this repo's issue tracker looked
   restricted for filing new issues directly, so a PR is the direct path
   in).
5. If merged and released to PyPI, follow up with a small version-bump PR to
   `home-assistant/core`: bump the pin in
   `homeassistant/components/anova/manifest.json`, regenerate via
   `python3 -m script.gen_requirements_all`, following the pattern of PR
   [#109508](https://github.com/home-assistant/core/pull/109508).

## Style preference

No em dashes in written output — PR description, code comments, commit
messages.
