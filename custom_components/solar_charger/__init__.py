"""SolarCharge — custom integration."""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path

import voluptuous as vol
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import websocket_api
from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)

from .const import (
    DOMAIN, PANEL_URL, PANEL_TITLE, PANEL_ICON,
    CONF_P1_POWER, CONF_SOLAR_POWER, CONF_CHARGER_SWITCH,
    CONF_CHARGER_POWER, CONF_MIN_SURPLUS, CONF_DELAY_ON,
    CONF_DELAY_OFF, CONF_EFFICIENCY, CONF_MAX_CHARGE_KW,
    DEFAULT_MIN_SURPLUS, DEFAULT_DELAY_ON, DEFAULT_DELAY_OFF,
    DEFAULT_EFFICIENCY, DEFAULT_MAX_CHARGE_KW,
)
from .storage import async_load_sessions, async_save_session, async_delete_all_sessions

_LOGGER = logging.getLogger(__name__)

# Entity IDs managed as native integration platforms (switch / number / text)
AUTOMATION_BOOL = "switch.solar_charger_automation_enabled"
SESSION_START   = "text.solar_charger_session_start"
SESSION_STOP    = "text.solar_charger_session_stop"
ENERGY_TODAY    = "number.solar_charger_energy_today"
ENERGY_BATT     = "number.solar_charger_energy_in_battery_today"
ENERGY_TOTAL    = "number.solar_charger_energy_total"
SESSION_MINS    = "number.solar_charger_session_duration_minutes"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    cfg = {**entry.data, **entry.options}
    hass.data[DOMAIN]["config"] = cfg
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    _register_websocket_commands(hass)
    await _register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, ["switch", "number", "text"])

    unsubs = _setup_automation_logic(hass, cfg, entry)
    hass.data[DOMAIN]["unsubs"] = unsubs

    # Schrijf config.json NADAT entities geregistreerd zijn zodat entity_ids bekend zijn
    await _async_write_config_with_ids(hass, cfg, entry)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.components.frontend import async_remove_panel as _fp_remove
    try:
        _fp_remove(hass, PANEL_URL)
    except Exception:
        pass
    for unsub in hass.data[DOMAIN].pop("unsubs", []):
        try:
            unsub()
        except Exception:
            pass
    hass.data[DOMAIN].pop("config", None)
    await hass.config_entries.async_unload_platforms(entry, ["switch", "number", "text"])
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Toon een melding bij permanente verwijdering van de integratie.

    Entities (switch / number / text) worden automatisch door HA verwijderd
    wanneer de config entry verwijderd wordt — geen handmatige cleanup nodig.
    De sessiehistoriek in HA storage blijft bewaard.
    """
    await hass.services.async_call(
        "persistent_notification", "create",
        {
            "title": "SolarCharge verwijderd",
            "message": (
                "De integratie is verwijderd. Alle bijhorende entiteiten "
                "(schakelaar, getallen, tekstvelden) zijn automatisch opgeruimd.\n\n"
                "Je laadsessiehistoriek blijft bewaard in HA storage "
                f"(`{DOMAIN}_sessions`) en zit in je HA-backups."
            ),
            "notification_id": f"{DOMAIN}_removed",
        },
        blocking=False,
    )
    _LOGGER.info("SolarCharge verwijderd")


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.info("SolarCharge: opties gewijzigd, herladen")
    await hass.config_entries.async_reload(entry.entry_id)


def _write_config_json(hass: HomeAssistant, cfg: dict) -> None:
    """Schrijf config.json naar www/solar_charger/ en kopieer panel.html als dat nog niet up-to-date is."""
    import shutil

    www_dir = Path(hass.config.config_dir) / "www" / "solar_charger"
    www_dir.mkdir(parents=True, exist_ok=True)

    # config.json — altijd overschrijven zodat sensor-IDs actueel zijn
    config_path = www_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)
    _LOGGER.debug("config.json geschreven naar %s", config_path)

    # panel.html — kopieer vanuit de integratiemap als de versie verschilt
    src = Path(__file__).parent / "www" / "panel.html"
    dst = www_dir / "panel.html"
    if src.exists() and (not dst.exists() or src.read_bytes() != dst.read_bytes()):
        shutil.copy2(src, dst)
        _LOGGER.info("panel.html gekopieerd naar %s", dst)


async def _async_write_config_with_ids(
    hass: HomeAssistant, cfg: dict, entry: ConfigEntry
) -> None:
    """Schrijf config.json inclusief de werkelijke entity-IDs uit het HA-register.

    De panel.html gebruikt deze IDs zodat het altijd de juiste entiteiten
    aanspreekt, ook na herinstallatie waarbij HA andere IDs kan toewijzen.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_ids: dict[str, str] = {}
    for domain, uid in [
        ("number", "solar_charger_min_surplus"),
        ("number", "solar_charger_delay_on"),
        ("number", "solar_charger_delay_off"),
        ("number", "solar_charger_efficiency"),
        ("number", "solar_charger_noplug_threshold"),
        ("number", "solar_charger_energy_today"),
        ("number", "solar_charger_energy_in_battery_today"),
        ("number", "solar_charger_energy_total"),
        ("number", "solar_charger_session_duration_minutes"),
        ("switch", "solar_charger_automation_enabled"),
        ("text",   "solar_charger_session_start"),
        ("text",   "solar_charger_session_stop"),
    ]:
        eid = registry.async_get_entity_id(domain, DOMAIN, uid)
        if eid:
            entity_ids[uid] = eid
    _LOGGER.debug("SolarCharge: entity_ids gevonden: %s", entity_ids)
    await hass.async_add_executor_job(
        _write_config_json, hass, {**cfg, "entity_ids": entity_ids}
    )


# ── WEBSOCKET COMMANDS ────────────────────────────────────────────────────────

def _register_websocket_commands(hass: HomeAssistant) -> None:
    """Registreer alle WebSocket commands voor het panel."""

    @websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_sessions"})
    @websocket_api.async_response
    async def ws_get_sessions(hass, connection, msg):
        sessions = await async_load_sessions(hass)
        connection.send_result(msg["id"], {"sessions": sessions})

    @websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/save_session",
        vol.Required("session"): dict,
    })
    @websocket_api.async_response
    async def ws_save_session(hass, connection, msg):
        await async_save_session(hass, msg["session"])
        connection.send_result(msg["id"], {"ok": True})

    @websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/delete_all_sessions"})
    @websocket_api.async_response
    async def ws_delete_all(hass, connection, msg):
        await async_delete_all_sessions(hass)
        connection.send_result(msg["id"], {"ok": True})

    websocket_api.async_register_command(hass, ws_get_sessions)
    websocket_api.async_register_command(hass, ws_save_session)
    websocket_api.async_register_command(hass, ws_delete_all)
    _LOGGER.debug("WebSocket commands geregistreerd")


# ── PANEL ─────────────────────────────────────────────────────────────────────

async def _register_panel(hass: HomeAssistant) -> None:
    from homeassistant.components.frontend import async_remove_panel
    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
    version = manifest.get("version", "0")
    panel_url = f"/local/solar_charger/panel.html?v={version}"
    _LOGGER.debug("Panel registratie gestart — url=%s", panel_url)
    try:
        async_remove_panel(hass, PANEL_URL)
    except Exception:
        pass
    try:
        async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_URL,
            config={"url": panel_url},
            require_admin=False,
        )
        _LOGGER.info("SolarCharge panel geregistreerd (%s)", panel_url)
    except Exception as err:
        _LOGGER.error("Panel registratie mislukt: %s", err, exc_info=True)


# ── AUTOMATION LOGIC ──────────────────────────────────────────────────────────

def _setup_automation_logic(
    hass: HomeAssistant, cfg: dict, entry: ConfigEntry
) -> list:
    """Implementeer de surplus-gestuurde laadautomatisering via HA event tracking.

    Retourneert een lijst van unsub-callbacks die op unload gecanceld moeten worden.
    """
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)

    def _eid(uid: str, domain: str, fallback: str) -> str:
        return registry.async_get_entity_id(domain, DOMAIN, uid) or fallback

    # Notificatieteksten op basis van de HA-taalinstelling
    _en = getattr(hass.config, "language", "nl").startswith("en")
    _TXT = {
        "title_start": "Battery charging (SolarCharge)" if _en else "Batterij laden (SolarCharge)",
        "msg_start":   "Started: {time} · Solar surplus: {w} W" if _en else "Gestart: {time} · Zonne-overschot: {w} W",
        "title_stop":  "Battery charging stopped (SolarCharge)" if _en else "Batterij laden gestopt (SolarCharge)",
        "msg_stop":    "Stopped: {time} · {mins} min · {kwh} kWh charged" if _en else "Gestopt: {time} · {mins} min · {kwh} kWh geladen",
    }

    # Resolve actual entity IDs from the registry (robust against reinstall suffix changes)
    eid_automation  = _eid("solar_charger_automation_enabled",       "switch", AUTOMATION_BOOL)
    eid_session_start = _eid("solar_charger_session_start",          "text",   SESSION_START)
    eid_session_stop  = _eid("solar_charger_session_stop",           "text",   SESSION_STOP)
    eid_energy_today  = _eid("solar_charger_energy_today",           "number", ENERGY_TODAY)
    eid_energy_batt   = _eid("solar_charger_energy_in_battery_today","number", ENERGY_BATT)
    eid_energy_total  = _eid("solar_charger_energy_total",           "number", ENERGY_TOTAL)
    eid_session_mins  = _eid("solar_charger_session_duration_minutes","number", SESSION_MINS)
    eid_min_surplus   = _eid("solar_charger_min_surplus",            "number", "number.solar_charger_min_surplus")
    eid_delay_on      = _eid("solar_charger_delay_on",               "number", "number.solar_charger_delay_on")
    eid_delay_off     = _eid("solar_charger_delay_off",              "number", "number.solar_charger_delay_off")

    p1_sensor   = cfg[CONF_P1_POWER]
    switch      = cfg[CONF_CHARGER_SWITCH]
    charger_pwr = cfg.get(CONF_CHARGER_POWER, "")
    max_kw      = float(cfg.get(CONF_MAX_CHARGE_KW, DEFAULT_MAX_CHARGE_KW))
    min_surplus = int(cfg.get(CONF_MIN_SURPLUS, DEFAULT_MIN_SURPLUS))
    delay_on    = int(cfg.get(CONF_DELAY_ON, DEFAULT_DELAY_ON))
    delay_off   = int(cfg.get(CONF_DELAY_OFF, DEFAULT_DELAY_OFF))
    efficiency  = float(cfg.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)) / 100

    def _live_min_surplus() -> int:
        v = _float_state(eid_min_surplus)
        return int(v) if v is not None else min_surplus

    def _live_delay_on() -> int:
        v = _float_state(eid_delay_on)
        return int(v) if v is not None else delay_on

    def _live_delay_off() -> int:
        v = _float_state(eid_delay_off)
        return int(v) if v is not None else delay_off

    # Mutable containers voor pending timer-cancel callbacks
    _timers: dict[str, object] = {"on": None, "off": None}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _automation_active() -> bool:
        state = hass.states.get(eid_automation)
        # Als de entity niet bestaat, behandelen we automatisering als actief
        return state is None or state.state == "on"

    def _charger_state() -> str:
        state = hass.states.get(switch)
        return state.state if state else "off"

    def _p1_watts() -> float:
        state = hass.states.get(p1_sensor)
        try:
            return float(state.state)
        except (AttributeError, ValueError, TypeError):
            return 0.0

    def _charger_watts() -> float:
        if charger_pwr:
            state = hass.states.get(charger_pwr)
            try:
                return float(state.state)
            except (AttributeError, ValueError, TypeError):
                pass
        return max_kw * 1000 if _charger_state() == "on" else 0.0

    def _float_state(entity_id: str):
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # ── acties ───────────────────────────────────────────────────────────────

    async def _do_turn_on(now=None) -> None:
        """Definitieve check en lader inschakelen na delay_on seconden overschot."""
        _timers["on"] = None
        if not _automation_active():
            return
        if _p1_watts() > -_live_min_surplus():
            _LOGGER.debug("SolarCharge: turn-on timer verlopen maar overschot verdwenen")
            return
        if _charger_state() == "on":
            return

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=False
        )
        start_iso = datetime.now().isoformat()
        await hass.services.async_call(
            "text", "set_value",
            {"entity_id": eid_session_start, "value": start_iso},
            blocking=False,
        )
        surplus_w = round(-_p1_watts())
        hass.data[DOMAIN]["session_start_surplus"] = surplus_w
        await hass.services.async_call(
            "notify", "persistent_notification",
            {
                "title": _TXT["title_start"],
                "message": _TXT["msg_start"].format(time=datetime.now().strftime("%H:%M"), w=surplus_w),
            },
            blocking=False,
        )
        _LOGGER.info("SolarCharge: lader ingeschakeld (overschot %s W)", surplus_w)

    async def _do_turn_off(now=None) -> None:
        """Definitieve check, sessie-energie bijwerken en lader uitschakelen."""
        _timers["off"] = None
        if not _automation_active():
            return
        if _p1_watts() - _charger_watts() <= -_live_min_surplus():
            _LOGGER.debug("SolarCharge: turn-off timer verlopen maar overschot hersteld")
            return
        if _charger_state() == "off":
            return

        # Sessieduur berekenen
        duration_mins = 0
        start_iso = ""
        start_state = hass.states.get(eid_session_start)
        if start_state and start_state.state not in ("", "unknown", "unavailable"):
            start_iso = start_state.state
            try:
                start_dt = datetime.fromisoformat(start_iso)
                duration_mins = round((datetime.now() - start_dt).total_seconds() / 60)
            except ValueError:
                pass

        # Energie berekenen op basis van vermogen en duur
        kwh = round(_charger_watts() / 1000 * duration_mins / 60, 3) if duration_mins > 0 else 0.0
        kwh_batt = round(kwh * efficiency, 3)

        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch}, blocking=False
        )
        stop_iso = datetime.now().isoformat()
        await hass.services.async_call(
            "text", "set_value",
            {"entity_id": eid_session_stop, "value": stop_iso},
            blocking=False,
        )

        if duration_mins > 0 and hass.states.get(eid_session_mins) is not None:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": eid_session_mins, "value": duration_mins},
                blocking=False,
            )

        for eid_e, cur_kwh in [(eid_energy_today, kwh), (eid_energy_total, kwh)]:
            cur = _float_state(eid_e)
            if cur is not None:
                await hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": eid_e, "value": round(cur + cur_kwh, 3)},
                    blocking=False,
                )

        cur_batt = _float_state(eid_energy_batt)
        if cur_batt is not None:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": eid_energy_batt, "value": round(cur_batt + kwh_batt, 3)},
                blocking=False,
            )

        # Sla sessie op in HA storage (ook als het panel niet open is)
        start_surplus = hass.data.get(DOMAIN, {}).pop("session_start_surplus", 0)
        noplug_threshold_w = int(cfg.get("noplug_threshold_w", 50))
        noplug = _charger_watts() < noplug_threshold_w and duration_mins > 0
        if duration_mins > 0:
            await async_save_session(hass, {
                "startIso": start_iso,
                "stopIso": stop_iso,
                "durMins": duration_mins,
                "kwhMuur": round(kwh, 3),
                "kwhAccu": round(kwh_batt, 3),
                "startSurplus": start_surplus,
                "status": "noplug" if noplug else "ok",
            })

        await hass.services.async_call(
            "notify", "persistent_notification",
            {
                "title": _TXT["title_stop"],
                "message": _TXT["msg_stop"].format(time=datetime.now().strftime("%H:%M"), mins=duration_mins, kwh=kwh),
            },
            blocking=False,
        )
        _LOGGER.info("SolarCharge: lader uitgeschakeld (%s min, %s kWh)", duration_mins, kwh)

    # ── event handlers ────────────────────────────────────────────────────────

    @callback
    def _on_p1_change(event) -> None:
        """Reageer op wijzigingen van de P1 sensor.

        Surplus voldoende (p1 <= -min_surplus): schedule turn-on, cancel turn-off.
        Surplus onvoldoende: cancel turn-on, schedule turn-off als lader aan.
        """
        if not _automation_active():
            return

        p1 = _p1_watts()
        charger_on = _charger_state() == "on"
        # Trek laadvermogen af zodat de eigen consumptie van de lader geen turn-off triggert
        p1_adj = p1 - _charger_watts()

        cur_min_surplus = _live_min_surplus()
        if p1_adj <= -cur_min_surplus:
            # Voldoende overschot
            if _timers["off"]:
                _timers["off"]()
                _timers["off"] = None
                _LOGGER.debug("SolarCharge: turn-off timer geannuleerd (overschot hersteld)")
            if not charger_on and _timers["on"] is None:
                cur_delay_on = _live_delay_on()
                _timers["on"] = async_call_later(hass, cur_delay_on, _do_turn_on)
                _LOGGER.debug("SolarCharge: turn-on gepland over %s s (overschot %s W)", cur_delay_on, round(-p1_adj))
        else:
            # Onvoldoende overschot
            if _timers["on"]:
                _timers["on"]()
                _timers["on"] = None
                _LOGGER.debug("SolarCharge: turn-on timer geannuleerd (overschot weg)")
            if charger_on and _timers["off"] is None:
                cur_delay_off = _live_delay_off()
                _timers["off"] = async_call_later(hass, cur_delay_off, _do_turn_off)
                _LOGGER.debug("SolarCharge: turn-off gepland over %s s", cur_delay_off)

    async def _daily_reset(now: datetime) -> None:
        """Reset dagelijkse energie-tellers om middernacht."""
        for eid_e in [eid_energy_today, eid_energy_batt]:
            if hass.states.get(eid_e) is not None:
                await hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": eid_e, "value": 0},
                    blocking=False,
                )
        _LOGGER.info("SolarCharge: dagelijkse reset uitgevoerd")

    def _cancel_timers() -> None:
        """Cancel eventueel nog lopende delay-timers bij unload."""
        for key in ("on", "off"):
            if _timers[key]:
                _timers[key]()
                _timers[key] = None

    # ── registreer bij HA ─────────────────────────────────────────────────────

    @callback
    def _on_automation_toggle(event) -> None:
        """Annuleer pending timers onmiddellijk wanneer automatisering uitgeschakeld wordt."""
        new_state = event.data.get("new_state")
        if new_state and new_state.state == "off":
            _cancel_timers()
            _LOGGER.debug("SolarCharge: automatisering uitgeschakeld, timers geannuleerd")

    unsub_p1   = async_track_state_change_event(hass, [p1_sensor], _on_p1_change)
    unsub_auto = async_track_state_change_event(hass, [eid_automation], _on_automation_toggle)
    unsub_midnight = async_track_time_change(hass, _daily_reset, hour=0, minute=0, second=0)

    _LOGGER.info(
        "SolarCharge automatisering actief — min_surplus=%sW delay_on=%ss delay_off=%ss",
        min_surplus, delay_on, delay_off,
    )
    return [unsub_p1, unsub_auto, unsub_midnight, _cancel_timers]
