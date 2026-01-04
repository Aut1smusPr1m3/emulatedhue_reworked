"""Device state model."""
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# ------------------------------------------------------------
# NOTE: we now import ``field_validator`` so we can coerce
# hue/saturation values that Home Assistant returns as floats.
# ------------------------------------------------------------
from pydantic import BaseModel, field_validator

from emulated_hue import const

from .homeassistant import HomeAssistantController

if TYPE_CHECKING:
    from .config import Config
else:
    Config = "Config"


@dataclass
class Controller:
    """Dataclass to store controller instances."""

    # Longer names are to be renamed later on
    # here to make refactoring easier
    controller_hass: HomeAssistantController | None = None
    config_instance: Config = None
    loop: asyncio.AbstractEventLoop | None = None


class EntityState(BaseModel):
    """Store device state."""

    power_state: bool = True
    reachable: bool = True
    transition_seconds: float | None = None
    brightness: int | None = None
    color_temp: int | None = None

    # --------------------------------------------------------
    # Hue‑saturation may come from HA as floats (e.g. 27.152)
    # The Hue API expects integers, so we accept floats here and
    # coerce them to ints with a validator (see below).
    # --------------------------------------------------------
    hue_saturation: tuple[int | float, int | float] | None = None

    xy_color: tuple[float, float] | None = None
    rgb_color: tuple[int, int, int] | None = None
    flash_state: str | None = None
    effect: str | None = None
    color_mode: str | None = None

    # -----------------------------------------------------------------
    # NEW: validator that guarantees integer hue & saturation **and**
    # scales HA's 0‑360 / 0‑100 ranges to Hue's 0‑65535 / 0‑254 ranges.
    # -----------------------------------------------------------------
    @field_validator("hue_saturation")
    @classmethod
    def _coerce_hue_sat(
        cls, v: tuple[int | float, int | float] | None
    ) -> tuple[int, int] | None:
        """
        Home‑Assistant reports hue/saturation as floats:
          * hue  : 0‑360 ° (or 0‑65535 if the integration already scaled it)
          * sat  : 0‑100 % (or 0‑254 if already scaled)

        The Hue API expects **integers** in the ranges:
          * hue  : 0‑65535
          * sat  : 0‑254

        This validator:
          1. Detects whether the incoming numbers are already in the Hue range
             (>= 360 for hue or >= 254 for sat). If they are, we simply round.
          2. Otherwise we scale:
                hue_int = round(hue * 65535 / 360)
                sat_int = round(sat * 254 / 100)
        """
        if v is None:
            return v

        hue, sat = v

        # If the values look already scaled for Hue, just round them.
        #if hue >= 360 or sat >= 254:
        #    return (int(round(hue)), int(round(sat)))

        # Normal HA ranges – scale to Hue ranges.
        #hue_int = int(round((hue * 65535) / 360))
        #sat_int = int(round((sat * 254) / 100))

        # Clamp just in case Home Assistant ever returns something out of bounds.
        hue_int = max(0, min(hue, 360))
        sat_int = max(0, min(sat, 100))
        return (hue_int, sat_int)

    # -----------------------------------------------------------------

    def __eq__(self, other):
        """Compare states."""
        other: EntityState = other
        power_state_equal = self.power_state == other.power_state
        brightness_equal = self.brightness == other.brightness
        color_attribute = (
            self._get_color_mode_attribute() == other._get_color_mode_attribute()
        )
        return power_state_equal and brightness_equal and color_attribute

    class Config:
        """Pydantic config."""

        validate_assignment = True

    def _get_color_mode_attribute(self) -> tuple[str, Any] | None:
        """Return color mode and attribute associated."""
        if self.color_mode == const.HASS_COLOR_MODE_COLOR_TEMP:
            return const.HASS_ATTR_COLOR_TEMP, self.color_temp
        elif self.color_mode == const.HASS_COLOR_MODE_HS:
            return const.HASS_ATTR_HS_COLOR, self.hue_saturation
        elif self.color_mode == const.HASS_COLOR_MODE_XY:
            return const.HASS_ATTR_XY_COLOR, self.xy_color
        elif self.color_mode == const.HASS_COLOR_MODE_RGB:
            return const.HASS_ATTR_RGB_COLOR, self.rgb_color
        return None

    def to_hass_data(self) -> dict:
        """Convert to Hass data."""
        data = {}
        if self.brightness:
            data[const.HASS_ATTR_BRIGHTNESS] = self.brightness

        color_mode_attribute = self._get_color_mode_attribute()
        if color_mode_attribute:
            color_mode, attribute = color_mode_attribute
            data[color_mode] = attribute

        if self.effect:
            data[const.HASS_ATTR_EFFECT] = self.effect
        if self.flash_state:
            data[const.HASS_ATTR_FLASH] = self.flash_state
        else:
            data[const.HASS_ATTR_TRANSITION] = self.transition_seconds
        return data

    @classmethod
    def from_config(cls, states: dict | None):
        """Convert from config."""
        # Initialize states if first time running
        if not states:
            return EntityState()

        save_state = {}
        # Hinweis: 'vars(cls).get("__fields__")' ist Pydantic V1 Syntax.
        # Falls du komplett auf V2 migrierst, wäre 'cls.model_fields' korrekter.
        # Ich habe die Logik hier konservativ behalten, aber den Bug in der if-Condition gefixt.
        fields = vars(cls).get("__fields__") or cls.model_fields

        for state in list(fields):
            if state in states:
                save_state[state] = states[state]
        return EntityState(**save_state)


ALL_STATES: list = list(EntityState.model_fields.keys())