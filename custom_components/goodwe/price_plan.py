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
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, time
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ATTR_DEVICE_ID,
    CONF_NEG_PRICE_ENABLED,
    DOMAIN,
    SERVICE_SET_NEG_PRICE_PLAN,
    SERVICE_CONFIGURE_NEG_PRICE_PLAN,
    SERVICE_UPDATE_NEG_PRICE_PLANS,
)

_LOGGER = logging.getLogger(__name__)

# PSE API constants
PSE_API_URL = "https://api.raporty.pse.pl/api/rce-pln"
PSE_RETRY_START_HOUR = 14  # Start retry at 14:00
PSE_RETRY_START_MINUTE = 30  # Actually start at 14:30
PSE_RETRY_INTERVAL_MINUTES = 30  # Retry every 30 minutes

# Key for storing price plan config per entry in hass.data[DOMAIN]
NEG_PRICE_CONFIG_KEY = "neg_price_config"

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


async def _fetch_pse_prices(hass: HomeAssistant, business_date: date) -> list[float] | None:
    """Fetch RCE PLN prices from PSE API for a given date.

    Returns list of 96 prices (15-min slots) in PLN/MWh, or None if fetch fails.
    """
    session = async_get_clientsession(hass)
    date_str = business_date.strftime('%Y-%m-%d')
    params = {"$filter": f"business_date eq '{date_str}'"}

    try:
        async with session.get(PSE_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                _LOGGER.error("PSE API returned status %d for date %s", resp.status, date_str)
                return None

            data = await resp.json()
            values = data.get("value", [])

            if not values:
                _LOGGER.debug("PSE API returned no data for date %s (likely future date)", date_str)
                return None

            # Extract rce_pln prices (should be 96 records for a full day)
            prices = [float(item["rce_pln"]) for item in values]

            if len(prices) != 96:
                _LOGGER.warning(
                    "PSE API returned %d records for %s, expected 96. Using partial data.",
                    len(prices), date_str
                )

            _LOGGER.debug("Fetched %d prices from PSE API for %s (min=%.2f, max=%.2f PLN/MWh)",
                         len(prices), date_str, min(prices) if prices else 0, max(prices) if prices else 0)
            return prices

    except asyncio.TimeoutError:
        _LOGGER.error("Timeout fetching PSE API for date %s", date_str)
        return None
    except aiohttp.ClientError as err:
        _LOGGER.error("HTTP error fetching PSE API for date %s: %s", date_str, err)
        return None
    except (KeyError, ValueError, TypeError) as err:
        _LOGGER.error("Error parsing PSE API response for date %s: %s", date_str, err)
        return None


def _extract_prices(hass: HomeAssistant, entity_id: str, attribute: str) -> list[float] | None:
    """Extract a price list from a HA entity attribute or state.

    Supports:
    - Nordpool/Tibber style: attribute is a list of dicts with 'value', 'price', or 'v' key
    - Generic list attribute: list of numeric values
    - Single numeric state (repeated for all 96 slots as fallback)

    Returns list of floats or None if extraction fails.
    """
    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.warning("Price entity '%s' not found in HA state.", entity_id)
        return None

    attr_val = state.attributes.get(attribute)
    if attr_val is not None and isinstance(attr_val, list) and len(attr_val) > 0:
        prices: list[float] = []
        for item in attr_val:
            if isinstance(item, dict):
                # Nordpool/Tibber: {"start": ..., "end": ..., "value": 1.23}
                val = item.get("value") or item.get("price") or item.get("v")
                if val is not None:
                    try:
                        prices.append(float(val))
                    except (TypeError, ValueError):
                        pass
            else:
                try:
                    prices.append(float(item))
                except (TypeError, ValueError):
                    pass
        if prices:
            return prices

    # Fallback: single numeric state value -> repeat for all 96 slots
    try:
        val = float(state.state)
        _LOGGER.debug(
            "Entity '%s' attribute '%s' not found or not a list; using state value %.4f for all slots.",
            entity_id, attribute, val
        )
        return [val] * 96
    except (ValueError, TypeError):
        _LOGGER.warning(
            "Cannot extract prices from entity '%s' attribute '%s' (state: %s).",
            entity_id, attribute, state.state
        )
        return None


async def _get_inverter_and_entry(hass: HomeAssistant, device_id: str):
    """Return (inverter, entry) for a given device_id."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ValueError(f"Device {device_id} not found")
    for entry_id, runtime_data in hass.data[DOMAIN].items():
        if device.identifiers == runtime_data.device_info.get("identifiers"):
            # Get the config entry to check neg_price_enabled
            from homeassistant.config_entries import current_entry
            entry = hass.config_entries.async_get_entry(entry_id)
            return runtime_data.inverter, entry
    raise ValueError(f"Inverter for device id {device_id} not found")


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


async def _update_from_config(hass: HomeAssistant, inverter, config: dict) -> None:
    """Read current prices from configured sources and write plan to inverter.

    Supports two source types:
    - "entity": read from HA entity attributes (Nordpool/Tibber/etc)
    - "rce_warsaw": fetch from PSE API (automatic, 15-min granularity)
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)

    sell_masks = None
    sell_tomorrow_masks = None
    buy_masks = None
    buy_tomorrow_masks = None

    source_type = config.get("source_type", "entity")
    sell_threshold = float(config.get("sell_threshold", 0.0))
    flip_sell = bool(config.get("flip_sell", False))
    buy_threshold = float(config.get("buy_threshold", 0.0))
    flip_buy = bool(config.get("flip_buy", False))
    buy_switch = config.get("buy_switch")

    if source_type == "rce_warsaw":
        # Fetch from PSE API (RCE Warsaw)
        _LOGGER.debug("Fetching prices from PSE API (RCE Warsaw)")

        today_prices = await _fetch_pse_prices(hass, today)
        if today_prices:
            # PSE returns 96 x 15-min slots, no need to specify slot_minutes
            sell_masks = build_mask(today_prices, sell_threshold, flip_sell, slot_minutes=15)
            _LOGGER.info("Built sell today mask from PSE API (%d prices)", len(today_prices))

            # Use same prices for buy plan if enabled
            if buy_switch is not None and buy_switch > 0:
                buy_masks = build_mask(today_prices, buy_threshold, flip_buy, slot_minutes=15)
                _LOGGER.info("Built buy today mask from PSE API (%d prices)", len(today_prices))

        tomorrow_prices = await _fetch_pse_prices(hass, tomorrow)
        if tomorrow_prices:
            sell_tomorrow_masks = build_mask(tomorrow_prices, sell_threshold, flip_sell, slot_minutes=15)
            _LOGGER.info("Built sell tomorrow mask from PSE API (%d prices)", len(tomorrow_prices))

            if buy_switch is not None and buy_switch > 0:
                buy_tomorrow_masks = build_mask(tomorrow_prices, buy_threshold, flip_buy, slot_minutes=15)
                _LOGGER.info("Built buy tomorrow mask from PSE API (%d prices)", len(tomorrow_prices))
        else:
            _LOGGER.warning("PSE API: Tomorrow's prices not yet available (will retry later)")

    else:  # source_type == "entity"
        # Legacy: read from HA entities
        slot_minutes = int(config.get("slot_minutes", 60))

        sell_entity = config.get("sell_entity_id")
        if sell_entity:
            today_prices = _extract_prices(hass, sell_entity, config.get("sell_today_attribute", "raw_today"))
            if today_prices:
                sell_masks = build_mask(today_prices, sell_threshold, flip_sell, slot_minutes)
                _LOGGER.debug("Built sell today mask from entity '%s': %s", sell_entity, sell_masks)

            tomorrow_prices = _extract_prices(hass, sell_entity, config.get("sell_tomorrow_attribute", "raw_tomorrow"))
            if tomorrow_prices:
                sell_tomorrow_masks = build_mask(tomorrow_prices, sell_threshold, flip_sell, slot_minutes)
                _LOGGER.debug("Built sell tomorrow mask from entity '%s': %s", sell_entity, sell_tomorrow_masks)

        buy_entity = config.get("buy_entity_id")
        if buy_entity:
            today_prices = _extract_prices(hass, buy_entity, config.get("buy_today_attribute", "raw_today"))
            if today_prices:
                buy_masks = build_mask(today_prices, buy_threshold, flip_buy, slot_minutes)
                _LOGGER.debug("Built buy today mask from entity '%s': %s", buy_entity, buy_masks)

            tomorrow_prices = _extract_prices(hass, buy_entity, config.get("buy_tomorrow_attribute", "raw_tomorrow"))
            if tomorrow_prices:
                buy_tomorrow_masks = build_mask(tomorrow_prices, buy_threshold, flip_buy, slot_minutes)
                _LOGGER.debug("Built buy tomorrow mask from entity '%s': %s", buy_entity, buy_tomorrow_masks)

    await _write_plan(
        inverter, today,
        sell_masks, sell_tomorrow_masks,
        buy_switch, buy_masks, buy_tomorrow_masks,
        enable=None,
    )


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

SERVICE_CONFIGURE_NEG_PRICE_PLAN_SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): str,
    vol.Optional("source_type", default="entity"): vol.In(["entity", "rce_warsaw"]),
    # Entity source fields (used when source_type="entity")
    vol.Optional("sell_entity_id"): cv.entity_id,
    vol.Optional("sell_today_attribute", default="raw_today"): str,
    vol.Optional("sell_tomorrow_attribute", default="raw_tomorrow"): str,
    vol.Optional("buy_entity_id"): cv.entity_id,
    vol.Optional("buy_today_attribute", default="raw_today"): str,
    vol.Optional("buy_tomorrow_attribute", default="raw_tomorrow"): str,
    vol.Optional("slot_minutes", default=60): vol.In([15, 30, 60]),
    # Common fields for all source types
    vol.Optional("sell_threshold", default=0.0): vol.Coerce(float),
    vol.Optional("flip_sell", default=False): cv.boolean,
    vol.Optional("buy_threshold", default=0.0): vol.Coerce(float),
    vol.Optional("flip_buy", default=False): cv.boolean,
    vol.Optional("buy_switch", default=1): vol.In([0, 1, 2]),
})

SERVICE_UPDATE_NEG_PRICE_PLANS_SCHEMA = vol.Schema({
    vol.Optional(ATTR_DEVICE_ID): str,
})


async def _auto_update_rce_warsaw(hass: HomeAssistant) -> None:
    """Auto-update task for RCE Warsaw price plans.

    Runs daily starting at 14:30, retries every 30 minutes until tomorrow's data appears.
    """
    _LOGGER.debug("RCE Warsaw auto-update task triggered")

    for entry_id, runtime_data in hass.data[DOMAIN].items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or not entry.options.get(CONF_NEG_PRICE_ENABLED, False):
            continue

        config = getattr(runtime_data, NEG_PRICE_CONFIG_KEY, None)
        if not config or config.get("source_type") != "rce_warsaw":
            continue

        try:
            _LOGGER.info("Auto-updating RCE Warsaw prices for entry %s", entry_id)
            await _update_from_config(hass, runtime_data.inverter, config)
        except Exception as err:
            _LOGGER.error("Failed to auto-update RCE Warsaw prices for entry %s: %s", entry_id, err)


def _setup_auto_update_schedule(hass: HomeAssistant) -> None:
    """Set up daily auto-update schedule for RCE Warsaw (PSE API).

    Schedule:
    - Start at 14:30 daily
    - Retry every 30 minutes (14:30, 15:00, 15:30, ..., 23:30)
    - PSE publishes tomorrow's prices usually between 14:00-17:00
    """
    # Schedule at 14:30, 15:00, 15:30, ..., 23:30 (retry windows)
    retry_minutes = list(range(PSE_RETRY_START_MINUTE, 60, PSE_RETRY_INTERVAL_MINUTES))  # [30]
    retry_minutes.extend(list(range(0, 60, PSE_RETRY_INTERVAL_MINUTES)))  # [0, 30]

    for minute in retry_minutes:
        if minute >= PSE_RETRY_START_MINUTE or minute == 0:  # 14:30+ or every :00 after 15:00
            async_track_time_change(
                hass,
                lambda now: hass.async_create_task(_auto_update_rce_warsaw(hass)),
                hour=list(range(PSE_RETRY_START_HOUR, 24)),  # 14:00-23:00
                minute=[minute],
                second=[0],
            )

    _LOGGER.info("RCE Warsaw auto-update schedule configured (daily from 14:30, every 30 min)")


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

    async def async_configure_neg_price_plan(call: ServiceCall) -> None:
        """Service: save neg price plan config and apply immediately."""
        device_id = call.data[ATTR_DEVICE_ID]
        inverter, entry_id = await _find_inverter_for_device(device_id)

        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and not entry.options.get(CONF_NEG_PRICE_ENABLED, False):
            _LOGGER.warning(
                "Negative price plan feature not enabled for entry %s. "
                "Enable it in integration options to activate automatic updates.",
                entry_id,
            )

        config = {
            "source_type": call.data.get("source_type", "entity"),
            "sell_entity_id": call.data.get("sell_entity_id"),
            "sell_today_attribute": call.data.get("sell_today_attribute", "raw_today"),
            "sell_tomorrow_attribute": call.data.get("sell_tomorrow_attribute", "raw_tomorrow"),
            "sell_threshold": call.data.get("sell_threshold", 0.0),
            "flip_sell": call.data.get("flip_sell", False),
            "buy_entity_id": call.data.get("buy_entity_id"),
            "buy_today_attribute": call.data.get("buy_today_attribute", "raw_today"),
            "buy_tomorrow_attribute": call.data.get("buy_tomorrow_attribute", "raw_tomorrow"),
            "buy_threshold": call.data.get("buy_threshold", 0.0),
            "flip_buy": call.data.get("flip_buy", False),
            "buy_switch": call.data.get("buy_switch", 1),
            "slot_minutes": call.data.get("slot_minutes", 60),
        }

        # Store config in hass.data under the entry
        runtime_data = hass.data[DOMAIN].get(entry_id)
        if runtime_data is not None:
            if not hasattr(runtime_data, "__dict__"):
                _LOGGER.error("runtime_data does not support attribute storage.")
                return
            setattr(runtime_data, NEG_PRICE_CONFIG_KEY, config)
            _LOGGER.info("Neg price plan config saved for entry %s: %s", entry_id, config)

        # Apply immediately
        await _update_from_config(hass, inverter, config)
        _LOGGER.info("Negative price plan configured and applied for device %s.", device_id)

    async def async_update_neg_price_plans(call: ServiceCall) -> None:
        """Service: re-read prices and update plan registers for all (or one) configured inverters."""
        target_device_id = call.data.get(ATTR_DEVICE_ID)

        for entry_id, runtime_data in hass.data[DOMAIN].items():
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and not entry.options.get(CONF_NEG_PRICE_ENABLED, False):
                continue

            config = getattr(runtime_data, NEG_PRICE_CONFIG_KEY, None)
            if config is None:
                continue

            if target_device_id is not None:
                # Filter to specific device if requested
                device = dr.async_get(hass).async_get(target_device_id)
                if device is None or device.identifiers != runtime_data.device_info.get("identifiers"):
                    continue

            try:
                await _update_from_config(hass, runtime_data.inverter, config)
                _LOGGER.info("Neg price plan updated for entry %s.", entry_id)
            except Exception as err:
                _LOGGER.error("Failed to update neg price plan for entry %s: %s", entry_id, err)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_NEG_PRICE_PLAN,
        async_set_neg_price_plan,
        schema=SERVICE_SET_NEG_PRICE_PLAN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIGURE_NEG_PRICE_PLAN,
        async_configure_neg_price_plan,
        schema=SERVICE_CONFIGURE_NEG_PRICE_PLAN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_NEG_PRICE_PLANS,
        async_update_neg_price_plans,
        schema=SERVICE_UPDATE_NEG_PRICE_PLANS_SCHEMA,
    )

    # Set up auto-update schedule for RCE Warsaw (PSE API)
    _setup_auto_update_schedule(hass)

    _LOGGER.debug("Negative price plan services registered.")


async def async_unload_neg_price_services(hass: HomeAssistant) -> None:
    """Unload negative price plan services."""
    for svc in (SERVICE_SET_NEG_PRICE_PLAN, SERVICE_CONFIGURE_NEG_PRICE_PLAN, SERVICE_UPDATE_NEG_PRICE_PLANS):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
