"""Negative electric price plan management for GoodWe inverters.

Manages 96-bit bitmasks (6 x U16 registers) representing 24h in 15-min slots.
Bit=1 means the inverter should treat that slot as a negative/favorable price period.

Registers 47785-47812 layout:
  47785: neg_price_enable  (on/off switch for the whole feature)
  47786: rtc_today         (date: month<<8 | day)
  47787-47792: sell_today_1..6  (6 x U16 = 96 bits sell mask for today)
  47793: rtc_tomorrow      (date: month<<8 | day)
  47794-47799: sell_tomorrow_1..6
  47800: buy_switch        (0=disabled, 1=charge only, 2=charge+positive sell)
  47801-47806: buy_today_1..6
  47807-47812: buy_tomorrow_1..6

Architecture:
  - GoodWe integration: provides low-level service to write masks + midnight rollover
  - Price optimizer integration (external): fetches prices, builds masks, calls service
  - Midnight rollover: copies tomorrow → today at 00:00:30 daily
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change

from .const import (
    ATTR_DEVICE_ID,
    CONF_NEG_PRICE_ENABLED,
    DOMAIN,
    SERVICE_SET_NEG_PRICE_PLAN,
)

_LOGGER = logging.getLogger(__name__)

# Register names used for writing
_SELL_TODAY_REGS = [f"neg_price_sell_today_{i}" for i in range(1, 7)]
_SELL_TOMORROW_REGS = [f"neg_price_sell_tomorrow_{i}" for i in range(1, 7)]
_BUY_TODAY_REGS = [f"neg_price_buy_today_{i}" for i in range(1, 7)]
_BUY_TOMORROW_REGS = [f"neg_price_buy_tomorrow_{i}" for i in range(1, 7)]


def build_mask(prices: list[float], threshold: float, flip: bool, slot_minutes: int = 60) -> list[int]:
    """Build 6 U16 bitmask registers from a list of prices.

    Args:
        prices: price values per input slot (e.g. 24 entries for hourly, 96 for 15-min)
        threshold: prices strictly below threshold -> bit=1 (negative/favorable period)
        flip: if True, invert all bits (bit=1 for prices >= threshold instead)
        slot_minutes: granularity of input prices in minutes (15, 30, or 60)

    Returns:
        list of 6 integers (U16, LSB = earliest time slot within each register)
        Register 1 covers slots 0-15 (00:00-04:00), register 6 covers slots 80-95 (20:00-24:00)
    """
    slots_per_day = 96  # 24h x 4 slots/hour
    bits_per_input = max(1, slot_minutes // 15)

    # Expand input prices to 15-min granularity
    expanded: list[float] = []
    for price in prices:
        expanded.extend([float(price)] * bits_per_input)

    # Pad to 96 slots (or truncate if somehow over)
    if len(expanded) < slots_per_day:
        expanded.extend([0.0] * (slots_per_day - len(expanded)))
    expanded = expanded[:slots_per_day]

    regs = [0] * 6
    for i, price in enumerate(expanded):
        is_set = price < threshold
        if flip:
            is_set = not is_set
        if is_set:
            regs[i // 16] |= 1 << (i % 16)

    return regs


def encode_rtc_date(d: date) -> int:
    """Encode a date as (month << 8) | day for GoodWe RTC date registers."""
    return (d.month << 8) | d.day


async def _write_plan(inverter, today: date, sell_masks: list[int] | None, sell_tomorrow_masks: list[int] | None,
                      buy_switch: int | None, buy_masks: list[int] | None, buy_tomorrow_masks: list[int] | None,
                      enable: bool | None) -> None:
    """Write neg price plan registers to inverter."""
    tomorrow = today + timedelta(days=1)

    if enable is not None:
        await inverter.write_setting("neg_price_enable", 1 if enable else 0)

    if sell_masks is not None:
        await inverter.write_setting("neg_price_rtc_today", encode_rtc_date(today))
        for reg, val in zip(_SELL_TODAY_REGS, sell_masks):
            await inverter.write_setting(reg, val)

    if sell_tomorrow_masks is not None:
        await inverter.write_setting("neg_price_rtc_tomorrow", encode_rtc_date(tomorrow))
        for reg, val in zip(_SELL_TOMORROW_REGS, sell_tomorrow_masks):
            await inverter.write_setting(reg, val)

    if buy_switch is not None:
        await inverter.write_setting("neg_price_buy_switch", buy_switch)

    if buy_masks is not None:
        for reg, val in zip(_BUY_TODAY_REGS, buy_masks):
            await inverter.write_setting(reg, val)

    if buy_tomorrow_masks is not None:
        for reg, val in zip(_BUY_TOMORROW_REGS, buy_tomorrow_masks):
            await inverter.write_setting(reg, val)


SERVICE_SET_NEG_PRICE_PLAN_SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): str,
    vol.Optional("neg_price_enable"): cv.boolean,
    vol.Optional("sell_today_masks"): vol.All([vol.Coerce(int)], vol.Length(min=6, max=6)),
    vol.Optional("sell_tomorrow_masks"): vol.All([vol.Coerce(int)], vol.Length(min=6, max=6)),
    vol.Optional("buy_switch"): vol.In([0, 1, 2]),
    vol.Optional("buy_today_masks"): vol.All([vol.Coerce(int)], vol.Length(min=6, max=6)),
    vol.Optional("buy_tomorrow_masks"): vol.All([vol.Coerce(int)], vol.Length(min=6, max=6)),
    # Alternatively provide prices + threshold + flip to build masks on-the-fly
    vol.Optional("sell_today_prices"): [vol.Coerce(float)],
    vol.Optional("sell_tomorrow_prices"): [vol.Coerce(float)],
    vol.Optional("buy_today_prices"): [vol.Coerce(float)],
    vol.Optional("buy_tomorrow_prices"): [vol.Coerce(float)],
    vol.Optional("sell_threshold", default=0.0): vol.Coerce(float),
    vol.Optional("buy_threshold", default=0.0): vol.Coerce(float),
    vol.Optional("flip_sell", default=False): cv.boolean,
    vol.Optional("flip_buy", default=False): cv.boolean,
    vol.Optional("slot_minutes", default=60): vol.In([15, 30, 60]),
})


async def _midnight_rollover(hass: HomeAssistant) -> None:
    """Midnight rollover: copy tomorrow masks → today masks.

    At midnight (00:00:30), yesterday's "tomorrow" data becomes "today" data.
    This handler reads tomorrow mask sensors and writes them as today masks.
    Tomorrow masks are cleared (will be refilled by price optimizer integration).
    """
    _LOGGER.debug("Midnight rollover task triggered")

    today = date.today()

    for entry_id, runtime_data in hass.data[DOMAIN].items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or not entry.options.get(CONF_NEG_PRICE_ENABLED, False):
            continue

        try:
            inverter = runtime_data.inverter

            # Read tomorrow masks from sensors
            sell_tomorrow_entity = f"sensor.{runtime_data.device_info['name'].lower().replace(' ', '_')}_neg_price_sell_tomorrow_mask"
            buy_tomorrow_entity = f"sensor.{runtime_data.device_info['name'].lower().replace(' ', '_')}_neg_price_buy_tomorrow_mask"

            sell_tomorrow_state = hass.states.get(sell_tomorrow_entity)
            buy_tomorrow_state = hass.states.get(buy_tomorrow_entity)

            if sell_tomorrow_state is None or sell_tomorrow_state.state == "unavailable":
                _LOGGER.debug(
                    "No sell tomorrow mask available for midnight rollover (entry %s), skipping.",
                    entry_id
                )
                continue

            # Parse JSON masks
            try:
                sell_masks = json.loads(sell_tomorrow_state.state)
                buy_masks = json.loads(buy_tomorrow_state.state) if buy_tomorrow_state and buy_tomorrow_state.state != "unavailable" else [0, 0, 0, 0, 0, 0]
            except (json.JSONDecodeError, ValueError) as err:
                _LOGGER.error("Failed to parse tomorrow masks for entry %s: %s", entry_id, err)
                continue

            # Get buy_switch from inverter
            buy_switch_val = await inverter.read_setting("neg_price_buy_switch")

            # Write tomorrow → today
            await _write_plan(
                inverter, today,
                sell_masks=sell_masks,
                sell_tomorrow_masks=[0, 0, 0, 0, 0, 0],  # Clear tomorrow (will be refilled by optimizer)
                buy_switch=buy_switch_val,
                buy_masks=buy_masks if buy_switch_val > 0 else None,
                buy_tomorrow_masks=[0, 0, 0, 0, 0, 0] if buy_switch_val > 0 else None,
                enable=None,
            )

            _LOGGER.info("Midnight rollover complete for entry %s (tomorrow → today)", entry_id)

        except Exception as err:
            _LOGGER.error("Failed midnight rollover for entry %s: %s", entry_id, err)


def _setup_midnight_rollover(hass: HomeAssistant) -> None:
    """Set up daily midnight rollover schedule.

    Schedule: 00:00:30 daily (30 seconds after midnight)
    Copies tomorrow masks → today masks via Modbus write
    """
    async_track_time_change(
        hass,
        lambda now: hass.async_create_task(_midnight_rollover(hass)),
        hour=[0],
        minute=[0],
        second=[30],
    )
    _LOGGER.info("Midnight rollover schedule configured (daily at 00:00:30)")


async def async_setup_neg_price_services(hass: HomeAssistant) -> None:
    """Register negative electric price plan services."""

    if hass.services.has_service(DOMAIN, SERVICE_SET_NEG_PRICE_PLAN):
        return

    async def _find_inverter_for_device(device_id: str):
        """Return (inverter, entry_id) for a device_id."""
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise ValueError(f"Device {device_id} not found")
        for entry_id, runtime_data in hass.data[DOMAIN].items():
            if device.identifiers == runtime_data.device_info.get("identifiers"):
                return runtime_data.inverter, entry_id
        raise ValueError(f"Inverter for device id {device_id} not found")

    async def async_set_neg_price_plan(call: ServiceCall) -> None:
        """Service: write neg price plan registers immediately."""
        device_id = call.data[ATTR_DEVICE_ID]
        inverter, entry_id = await _find_inverter_for_device(device_id)

        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and not entry.options.get(CONF_NEG_PRICE_ENABLED, False):
            _LOGGER.warning(
                "Negative price plan feature not enabled for this inverter entry. "
                "Enable it in integration options first."
            )

        today = date.today()
        slot_minutes = call.data.get("slot_minutes", 60)
        sell_threshold = call.data.get("sell_threshold", 0.0)
        buy_threshold = call.data.get("buy_threshold", 0.0)
        flip_sell = call.data.get("flip_sell", False)
        flip_buy = call.data.get("flip_buy", False)

        # Build sell masks from prices if provided, otherwise use direct masks
        sell_masks = call.data.get("sell_today_masks")
        if sell_masks is None and call.data.get("sell_today_prices"):
            sell_masks = build_mask(call.data["sell_today_prices"], sell_threshold, flip_sell, slot_minutes)

        sell_tomorrow_masks = call.data.get("sell_tomorrow_masks")
        if sell_tomorrow_masks is None and call.data.get("sell_tomorrow_prices"):
            sell_tomorrow_masks = build_mask(call.data["sell_tomorrow_prices"], sell_threshold, flip_sell, slot_minutes)

        buy_masks = call.data.get("buy_today_masks")
        if buy_masks is None and call.data.get("buy_today_prices"):
            buy_masks = build_mask(call.data["buy_today_prices"], buy_threshold, flip_buy, slot_minutes)

        buy_tomorrow_masks = call.data.get("buy_tomorrow_masks")
        if buy_tomorrow_masks is None and call.data.get("buy_tomorrow_prices"):
            buy_tomorrow_masks = build_mask(call.data["buy_tomorrow_prices"], buy_threshold, flip_buy, slot_minutes)

        enable = call.data.get("neg_price_enable")
        buy_switch = call.data.get("buy_switch")

        await _write_plan(
            inverter, today,
            sell_masks, sell_tomorrow_masks,
            buy_switch, buy_masks, buy_tomorrow_masks,
            enable,
        )
        _LOGGER.info("Negative price plan written to inverter %s.", device_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_NEG_PRICE_PLAN,
        async_set_neg_price_plan,
        schema=SERVICE_SET_NEG_PRICE_PLAN_SCHEMA,
    )

    # Set up midnight rollover schedule
    _setup_midnight_rollover(hass)

    _LOGGER.debug("Negative price plan services registered.")


async def async_unload_neg_price_services(hass: HomeAssistant) -> None:
    """Unload negative price plan services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_NEG_PRICE_PLAN):
        hass.services.async_remove(DOMAIN, SERVICE_SET_NEG_PRICE_PLAN)
