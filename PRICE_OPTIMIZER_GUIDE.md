# Price Optimizer Integration Guide

This guide explains how to create a **price optimizer integration** that works with the GoodWe integration to manage negative electric price plans.

## Architecture

### GoodWe Integration (this integration)
- **Provides**: Low-level service to write price plan masks to the inverter
- **Provides**: 4 sensor entities showing current masks in the inverter
- **Handles**: Midnight rollover (copies tomorrow → today at 00:00:30 automatically)
- **Provides**: `neg_price_enable` switch in integration options

### Price Optimizer Integration (your integration)
- **Fetches**: Electricity prices from external sources (PSE API, Nordpool, Tibber, etc.)
- **Builds**: 96-bit bitmasks from prices using thresholds
- **Calls**: `goodwe.set_neg_price_plan` service with tomorrow's masks
- **Schedules**: Daily updates when new prices become available

**The price optimizer does NOT need to handle midnight rollover** - GoodWe integration does this automatically.

## How It Works

### Daily Flow

1. **14:30 - Price Optimizer** (your integration):
   - Fetches today + tomorrow prices from your source
   - Builds masks using `build_mask()` function
   - Calls `goodwe.set_neg_price_plan` with `sell_tomorrow_masks` and `buy_tomorrow_masks`
   - GoodWe integration writes masks to inverter registers

2. **00:00:30 - Midnight Rollover** (automatic, handled by GoodWe):
   - Reads tomorrow mask sensors
   - Writes them as today masks (Modbus write)
   - Clears tomorrow masks (will be refilled by your integration)

3. **Next day 14:30** - Cycle repeats

### GoodWe Sensors

The GoodWe integration provides 4 read-only sensors:
- `sensor.goodwe_neg_price_sell_today_mask` - JSON array of 6 integers
- `sensor.goodwe_neg_price_sell_tomorrow_mask` - JSON array of 6 integers
- `sensor.goodwe_neg_price_buy_today_mask` - JSON array of 6 integers
- `sensor.goodwe_neg_price_buy_tomorrow_mask` - JSON array of 6 integers

These show the current state of masks in the inverter.

## Building Bitmasks

### Understanding the 96-bit Mask

- **96 slots** × 15 minutes = 24 hours
- **6 registers** × 16 bits = 96 bits
- Slot 0 = 00:00-00:15, Slot 1 = 00:15-00:30, ..., Slot 95 = 23:45-00:00
- **Bit=1** means "favorable period" (price below threshold)

### Mask Structure

```
Register 1 (47787): Slots 0-15   (00:00-04:00)
Register 2 (47788): Slots 16-31  (04:00-08:00)
Register 3 (47789): Slots 32-47  (08:00-12:00)
Register 4 (47790): Slots 48-63  (12:00-16:00)
Register 5 (47791): Slots 64-79  (16:00-20:00)
Register 6 (47792): Slots 80-95  (20:00-24:00)
```

Each register: **LSB = earliest time**, MSB = latest time

### build_mask() Function

You can copy this helper function from `goodwe/price_plan.py`:

```python
def build_mask(prices: list[float], threshold: float, flip: bool, slot_minutes: int = 60) -> list[int]:
    """Build 6 U16 bitmask registers from a list of prices.

    Args:
        prices: price values per input slot (e.g. 24 entries for hourly, 96 for 15-min)
        threshold: prices strictly below threshold -> bit=1 (negative/favorable period)
        flip: if True, invert all bits (bit=1 for prices >= threshold instead)
        slot_minutes: granularity of input prices in minutes (15, 30, or 60)

    Returns:
        list of 6 integers (U16, LSB = earliest time slot within each register)
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
```

### Example: Hourly Prices

```python
# 24 hourly prices (PLN/MWh or EUR/MWh)
hourly_prices = [
    334.15, 340.22, 350.10, 360.00,  # 00:00-04:00
    400.50, 420.30, 450.00, 480.20,  # 04:00-08:00
    # ... 16 more hours
]

# Sell plan: prices < 400 PLN/MWh are favorable (don't sell during these periods)
sell_threshold = 400.0
flip_sell = False  # bit=1 when price < threshold

sell_masks = build_mask(hourly_prices, sell_threshold, flip_sell, slot_minutes=60)
# Returns: [12345, 23456, 34567, 45678, 56789, 67890]  # 6 integers

# Buy plan: prices < 450 PLN/MWh are favorable (charge battery)
buy_threshold = 450.0
flip_buy = False

buy_masks = build_mask(hourly_prices, buy_threshold, flip_buy, slot_minutes=60)
```

### Example: 15-minute Prices (PSE API)

```python
# 96 prices (15-min granularity)
prices_15min = [443.29, 440.15, ..., 380.50]  # 96 values

# Build masks
sell_masks = build_mask(prices_15min, 400.0, False, slot_minutes=15)
buy_masks = build_mask(prices_15min, 450.0, False, slot_minutes=15)
```

## Calling the Service

### Service Call Structure

```yaml
service: goodwe.set_neg_price_plan
data:
  device_id: <your_inverter_device_id>
  sell_tomorrow_masks: [12345, 23456, 34567, 45678, 56789, 67890]
  buy_tomorrow_masks: [12345, 23456, 34567, 45678, 56789, 67890]
  buy_switch: 1  # 0=disabled, 1=charge only, 2=charge+sell
```

### From Python (in your integration)

```python
from homeassistant.core import HomeAssistant

async def update_price_plan(hass: HomeAssistant, device_id: str, sell_masks: list[int], buy_masks: list[int]):
    """Call goodwe service to update tomorrow's price plan."""
    await hass.services.async_call(
        "goodwe",
        "set_neg_price_plan",
        {
            "device_id": device_id,
            "sell_tomorrow_masks": sell_masks,
            "buy_tomorrow_masks": buy_masks,
            "buy_switch": 1,  # Charge battery only
        },
        blocking=True,
    )
```

## Example: PSE API Price Optimizer

Complete example integration that fetches prices from PSE API (Polish RCE Warsaw):

```python
"""PSE Price Optimizer for GoodWe Integration."""
import aiohttp
import asyncio
from datetime import date, timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

PSE_API_URL = "https://api.raporty.pse.pl/api/rce-pln"

async def fetch_pse_prices(session: aiohttp.ClientSession, business_date: date) -> list[float] | None:
    """Fetch RCE PLN prices from PSE API."""
    params = {"$filter": f"business_date eq '{business_date.strftime('%Y-%m-%d')}'"}
    try:
        async with session.get(PSE_API_URL, params=params, timeout=15) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return [float(item["rce_pln"]) for item in data.get("value", [])]
    except Exception:
        return None

def build_mask(prices: list[float], threshold: float, flip: bool, slot_minutes: int = 60) -> list[int]:
    """Build bitmask from prices (copy from goodwe/price_plan.py)."""
    # ... (see above)
    pass

async def update_pse_price_plan(hass: HomeAssistant):
    """Fetch PSE prices and update GoodWe price plan."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    async with aiohttp.ClientSession() as session:
        # Fetch tomorrow's prices
        tomorrow_prices = await fetch_pse_prices(session, tomorrow)
        if not tomorrow_prices or len(tomorrow_prices) != 96:
            _LOGGER.warning("PSE API: Tomorrow's prices not available yet")
            return

        # Build masks
        sell_threshold = 400.0  # PLN/MWh
        buy_threshold = 450.0

        sell_masks = build_mask(tomorrow_prices, sell_threshold, False, slot_minutes=15)
        buy_masks = build_mask(tomorrow_prices, buy_threshold, False, slot_minutes=15)

        # Call goodwe service
        await hass.services.async_call(
            "goodwe",
            "set_neg_price_plan",
            {
                "device_id": "your_inverter_device_id",  # Get from config
                "sell_tomorrow_masks": sell_masks,
                "buy_tomorrow_masks": buy_masks,
                "buy_switch": 1,
            },
            blocking=True,
        )
        _LOGGER.info("PSE price plan updated for tomorrow")

async def setup_pse_scheduler(hass: HomeAssistant):
    """Set up daily PSE price fetch at 14:30."""
    async_track_time_change(
        hass,
        lambda now: hass.async_create_task(update_pse_price_plan(hass)),
        hour=[14, 15, 16, 17, 18, 19, 20, 21, 22, 23],  # 14:00-23:00
        minute=[30, 0],  # Every 30 minutes
        second=[0],
    )
    _LOGGER.info("PSE price optimizer scheduler configured")
```

## Service Parameters Reference

### Required
- `device_id` (string): GoodWe inverter device ID

### Mask Options (choose one)

**Option 1: Direct masks** (recommended for price optimizers)
- `sell_today_masks` (list of 6 ints): Sell plan for today
- `sell_tomorrow_masks` (list of 6 ints): Sell plan for tomorrow
- `buy_today_masks` (list of 6 ints): Buy plan for today
- `buy_tomorrow_masks` (list of 6 ints): Buy plan for tomorrow

**Option 2: Prices** (auto-build masks)
- `sell_today_prices` (list of floats): Prices for today
- `sell_tomorrow_prices` (list of floats): Prices for tomorrow
- `buy_today_prices` (list of floats): Buy prices for today
- `buy_tomorrow_prices` (list of floats): Buy prices for tomorrow
- `sell_threshold` (float, default 0.0): Threshold for sell plan
- `buy_threshold` (float, default 0.0): Threshold for buy plan
- `flip_sell` (bool, default false): Invert sell mask
- `flip_buy` (bool, default false): Invert buy mask
- `slot_minutes` (int, default 60): 15, 30, or 60

### Other
- `buy_switch` (int): 0=disabled, 1=charge battery only, 2=charge+positive sell
- `neg_price_enable` (bool): Enable/disable the feature on inverter

## Tips

1. **Always write tomorrow's masks**: Let GoodWe handle midnight rollover
2. **Retry logic**: PSE/Nordpool publish tomorrow prices at different times (14:00-17:00 typically)
3. **Separate thresholds**: Use different thresholds for sell and buy plans
4. **Check sensors**: Monitor the 4 mask sensors to verify writes succeeded
5. **Enable feature**: Users must enable `neg_price_enabled` in GoodWe integration options first

## Testing

Test your integration:

```yaml
# Developer Tools → Services
service: goodwe.set_neg_price_plan
data:
  device_id: <your_device_id>
  sell_tomorrow_masks: [0, 0, 0, 65535, 65535, 65535]  # All 1s for last 12 hours
  buy_tomorrow_masks: [65535, 65535, 65535, 0, 0, 0]   # All 1s for first 12 hours
  buy_switch: 1
```

Check sensors:
- `sensor.goodwe_neg_price_sell_tomorrow_mask` should show `[0, 0, 0, 65535, 65535, 65535]`
- `sensor.goodwe_neg_price_buy_tomorrow_mask` should show `[65535, 65535, 65535, 0, 0, 0]`

Wait until midnight, then check:
- `sensor.goodwe_neg_price_sell_today_mask` should show `[0, 0, 0, 65535, 65535, 65535]`
- Tomorrow masks should be cleared to `[0, 0, 0, 0, 0, 0]`

## Questions?

Open an issue at: https://github.com/TPPS999/home-assistant-goodwe-inverter/issues
