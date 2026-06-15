"""Config flow voor SolarCharge — wizard bij installatie en via Opties."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    DOMAIN,
    CONF_P1_POWER,
    CONF_SOLAR_POWER,
    CONF_CHARGER_SWITCH,
    CONF_CHARGER_POWER,
    CONF_MIN_SURPLUS,
    CONF_DELAY_ON,
    CONF_DELAY_OFF,
    CONF_EFFICIENCY,
    CONF_MAX_CHARGE_KW,
    DEFAULT_MIN_SURPLUS,
    DEFAULT_DELAY_ON,
    DEFAULT_DELAY_OFF,
    DEFAULT_EFFICIENCY,
    DEFAULT_MAX_CHARGE_KW,
)


def _power_sensor_selector() -> EntitySelector:
    """Selector die alleen power-sensoren toont."""
    return EntitySelector(
        EntitySelectorConfig(domain="sensor", device_class="power")
    )


def _any_sensor_selector() -> EntitySelector:
    """Selector voor alle sensoren (fallback als device_class niet overeenkomt)."""
    return EntitySelector(EntitySelectorConfig(domain="sensor"))


def _switch_selector() -> EntitySelector:
    """Selector voor switches."""
    return EntitySelector(EntitySelectorConfig(domain="switch"))


def _number_selector(min_val: float, max_val: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_val,
            max=max_val,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


class SolarCarChargerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Wizard bij eerste installatie van SolarCharge."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    async def async_step_user(self, user_input=None):
        """Stap 1: P1 sensor selecteren."""
        return await self.async_step_p1_sensor(user_input)

    async def async_step_p1_sensor(self, user_input=None):
        errors = {}
        if user_input is not None:
            entity_id = user_input[CONF_P1_POWER]
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[CONF_P1_POWER] = "invalid_sensor"
            else:
                self._data.update(user_input)
                return await self.async_step_solar_sensor()

        current = self._data.get(CONF_P1_POWER, "")
        schema = vol.Schema({
            vol.Required(CONF_P1_POWER, default=current): _power_sensor_selector(),
        })
        return self.async_show_form(
            step_id="p1_sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_solar_sensor(self, user_input=None):
        """Stap 2: Zonnepanelen sensor."""
        errors = {}
        if user_input is not None:
            entity_id = user_input[CONF_SOLAR_POWER]
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[CONF_SOLAR_POWER] = "invalid_sensor"
            else:
                self._data.update(user_input)
                return await self.async_step_charger_sensors()

        current = self._data.get(CONF_SOLAR_POWER, "")
        schema = vol.Schema({
            vol.Required(CONF_SOLAR_POWER, default=current): _power_sensor_selector(),
        })
        return self.async_show_form(
            step_id="solar_sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_charger_sensors(self, user_input=None):
        """Stap 3: Lader schakelaar en energiemeter."""
        errors = {}
        if user_input is not None:
            sw = user_input[CONF_CHARGER_SWITCH]
            if self.hass.states.get(sw) is None:
                errors[CONF_CHARGER_SWITCH] = "invalid_switch"
            else:
                self._data.update(user_input)
                return await self.async_step_thresholds()

        schema = vol.Schema({
            vol.Required(
                CONF_CHARGER_SWITCH,
                default=self._data.get(CONF_CHARGER_SWITCH, ""),
            ): _switch_selector(),
            vol.Optional(
                CONF_CHARGER_POWER,
                default=self._data.get(CONF_CHARGER_POWER, ""),
            ): _power_sensor_selector(),
            vol.Required(
                CONF_MAX_CHARGE_KW,
                default=self._data.get(CONF_MAX_CHARGE_KW, DEFAULT_MAX_CHARGE_KW),
            ): _number_selector(1.0, 22.0, 0.1, "kW"),
        })
        return self.async_show_form(
            step_id="charger_sensors",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_thresholds(self, user_input=None):
        """Stap 4: Drempelwaarden en vertragingen."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="SolarCharge",
                data=self._data,
            )

        schema = vol.Schema({
            vol.Required(
                CONF_MIN_SURPLUS,
                default=self._data.get(CONF_MIN_SURPLUS, DEFAULT_MIN_SURPLUS),
            ): _number_selector(0, 5000, 50, "W"),
            vol.Required(
                CONF_DELAY_ON,
                default=self._data.get(CONF_DELAY_ON, DEFAULT_DELAY_ON),
            ): _number_selector(30, 600, 30, "s"),
            vol.Required(
                CONF_DELAY_OFF,
                default=self._data.get(CONF_DELAY_OFF, DEFAULT_DELAY_OFF),
            ): _number_selector(30, 600, 30, "s"),
            vol.Required(
                CONF_EFFICIENCY,
                default=self._data.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY),
            ): _number_selector(70, 100, 1, "%"),
        })
        return self.async_show_form(
            step_id="thresholds",
            data_schema=schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Geef de options flow terug (herconfiguratiewizard)."""
        return SolarCarChargerOptionsFlow(config_entry)


class SolarCarChargerOptionsFlow(config_entries.OptionsFlow):
    """Options flow — dezelfde wizard opnieuw via de 'Opties' knop.
    
    Alle stappen zijn vooringevuld met de huidige waarden zodat de gebruiker
    alleen hoeft te wijzigen wat nodig is.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        # Begin met kopie van huidige config + eventuele eerder opgeslagen opties
        self._data: dict = {**config_entry.data, **config_entry.options}

    async def async_step_init(self, user_input=None):
        """Options flow start ook bij stap 1."""
        return await self.async_step_p1_sensor(user_input)

    async def async_step_p1_sensor(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_P1_POWER]) is None:
                errors[CONF_P1_POWER] = "invalid_sensor"
            else:
                self._data.update(user_input)
                return await self.async_step_solar_sensor()

        schema = vol.Schema({
            vol.Required(
                CONF_P1_POWER,
                default=self._data.get(CONF_P1_POWER, ""),
            ): _power_sensor_selector(),
        })
        return self.async_show_form(
            step_id="p1_sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_solar_sensor(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_SOLAR_POWER]) is None:
                errors[CONF_SOLAR_POWER] = "invalid_sensor"
            else:
                self._data.update(user_input)
                return await self.async_step_charger_sensors()

        schema = vol.Schema({
            vol.Required(
                CONF_SOLAR_POWER,
                default=self._data.get(CONF_SOLAR_POWER, ""),
            ): _power_sensor_selector(),
        })
        return self.async_show_form(
            step_id="solar_sensor",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_charger_sensors(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_CHARGER_SWITCH]) is None:
                errors[CONF_CHARGER_SWITCH] = "invalid_switch"
            else:
                self._data.update(user_input)
                return await self.async_step_thresholds()

        schema = vol.Schema({
            vol.Required(
                CONF_CHARGER_SWITCH,
                default=self._data.get(CONF_CHARGER_SWITCH, ""),
            ): _switch_selector(),
            vol.Optional(
                CONF_CHARGER_POWER,
                default=self._data.get(CONF_CHARGER_POWER, ""),
            ): _power_sensor_selector(),
            vol.Required(
                CONF_MAX_CHARGE_KW,
                default=self._data.get(CONF_MAX_CHARGE_KW, DEFAULT_MAX_CHARGE_KW),
            ): _number_selector(1.0, 22.0, 0.1, "kW"),
        })
        return self.async_show_form(
            step_id="charger_sensors",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_thresholds(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            # Sla op als options — overschrijft vorige opties, behoudt originele data
            return self.async_create_entry(title="", data=self._data)

        # Lees huidige waarden uit de entiteitstoestanden (kunnen afwijken van entry.options
        # als de gebruiker ze via het panel gewijzigd heeft)
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)

        def _live(uid: str, conf_key: str, default: float) -> float:
            entity_id = registry.async_get_entity_id("number", DOMAIN, uid) or f"number.{uid}"
            state = self.hass.states.get(entity_id)
            if state and state.state not in (None, "unknown", "unavailable"):
                try:
                    return float(state.state)
                except ValueError:
                    pass
            return self._data.get(conf_key, default)

        schema = vol.Schema({
            vol.Required(
                CONF_MIN_SURPLUS,
                default=_live("solar_charger_min_surplus", CONF_MIN_SURPLUS, DEFAULT_MIN_SURPLUS),
            ): _number_selector(0, 5000, 50, "W"),
            vol.Required(
                CONF_DELAY_ON,
                default=_live("solar_charger_delay_on", CONF_DELAY_ON, DEFAULT_DELAY_ON),
            ): _number_selector(30, 600, 30, "s"),
            vol.Required(
                CONF_DELAY_OFF,
                default=_live("solar_charger_delay_off", CONF_DELAY_OFF, DEFAULT_DELAY_OFF),
            ): _number_selector(30, 600, 30, "s"),
            vol.Required(
                CONF_EFFICIENCY,
                default=_live("solar_charger_efficiency", CONF_EFFICIENCY, DEFAULT_EFFICIENCY),
            ): _number_selector(70, 100, 1, "%"),
        })
        return self.async_show_form(
            step_id="thresholds",
            data_schema=schema,
        )
