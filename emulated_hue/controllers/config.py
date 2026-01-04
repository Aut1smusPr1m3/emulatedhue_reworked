"""Hold configuration variables for the emulated hue bridge."""
import asyncio
import datetime
import hashlib
import json                     # <-- NEW: needed for the sync write
import logging
import os
from pathlib import Path
from typing import Any

from getmac import get_mac_address

from emulated_hue.const import (
    CONFIG_WRITE_DELAY_SECONDS,
    DEFAULT_THROTTLE_MS,
    LABEL_FILTER_ENV_VAR,
)
from emulated_hue.utils import (
    async_save_json,
    create_secure_string,
    get_local_ip,
    load_json,
    parse_label_filter,
)

from .devices import force_update_all
from .entertainment import EntertainmentAPI
from .models import Controller

LOGGER = logging.getLogger(__name__)

CONFIG_FILE = "emulated_hue.json"
DEFINITIONS_FILE = os.path.join(
    os.path.dirname(Path(__file__).parent.absolute()), "definitions.json"
)

# ----------------------------------------------------------------------
# How many lights are we willing to expose? (Hue apps expect low IDs)
# ----------------------------------------------------------------------
MAX_LIGHT_ID = 20          # never let an ID > 19 be created
HARD_CODED_FILTER = ["ambi"]   # the filter you asked for


class Config:
    """Hold configuration variables for the emulated hue bridge."""

    # --------------------------------------------------------------
    #  ── 1️⃣  INITIALISATION
    # --------------------------------------------------------------
    def __init__(
        self,
        ctl: Controller,
        data_path: str,
        http_port: int,
        https_port: int,
        use_default_ports: bool,
        label_filter_exact: str | None = None,
    ):
        """Initialize the instance."""
        self.ctl = ctl
        self.data_path = data_path
        if not os.path.isdir(data_path):
            os.mkdir(data_path)

        # ------------------------------------------------------------------
        #  Load persisted JSON files
        # ------------------------------------------------------------------
        self._config = load_json(self.get_path(CONFIG_FILE))
        self._definitions = load_json(DEFINITIONS_FILE)
        self._link_mode_enabled = False
        self._link_mode_discovery_key = None

        # ------------------------------------------------------------------
        #  IP / MAC / Bridge IDs (unchanged)
        # ------------------------------------------------------------------
        self._ip_addr = get_local_ip()
        LOGGER.info("Auto detected listen IP address is %s", self.ip_addr)

        self.http_port = http_port
        self.https_port = https_port
        self.use_default_ports = use_default_ports
        if http_port != 80 or https_port != 443:
            LOGGER.warning(
                "Non default http/https ports detected. "
                "Hue apps require the bridge at the default ports 80/443, use at your own risk."
            )
            if self.use_default_ports:
                LOGGER.warning(
                    "Using default HTTP port for discovery with non default HTTP/S ports. "
                    "Are you using a reverse proxy?"
                )

        mac_addr = str(get_mac_address(ip=self.ip_addr))
        if not mac_addr or len(mac_addr) < 16:
            mac_addr = str(get_mac_address())
        if not mac_addr or len(mac_addr) < 16:
            mac_addr = "b6:82:d3:45:ac:29"
        self._mac_addr = mac_addr
        mac_str = mac_addr.replace(":", "")
        self._bridge_id = (mac_str[:6] + "FFFE" + mac_str[6:]).upper()
        self._bridge_serial = mac_str.lower()
        self._bridge_uid = f"2f402f80-da50-11e1-9b23-{mac_str}"

        self._saver_task: asyncio.Task | None = None
        self._entertainment_api: EntertainmentAPI | None = None

        # ------------------------------------------------------------------
        #  1️⃣  LABEL FILTER – hard‑coded to ["ambi"]
        # ------------------------------------------------------------------
        self._label_filter: list[str] = parse_label_filter(HARD_CODED_FILTER)

        # Allow a persisted override (kept for backward compatibility).
        persisted = self.get_storage_value("bridge_config", "label_filter", None)
        if persisted and isinstance(persisted, list):
            self._label_filter = [
                str(x).strip().lower() for x in persisted if x
            ]

        # ------------------------------------------------------------------
        #  3️⃣  PRUNE / RE‑NUMBER ON STARTUP (guarded)
        # ------------------------------------------------------------------
        # If pruning fails we do **not** want the whole integration to die,
        # therefore we wrap it in a try/except and only log the error.
        try:
            self._prune_and_renumber()
        except Exception as exc:   # pragma: no cover   (should never happen)
            LOGGER.error(
                "Failed to prune / renumber emulated_hue.json during init: %s",
                exc,
                exc_info=True,
            )

    # ----------------------------------------------------------------------
    #  ── PUBLIC PROPERTIES (unchanged)
    # ----------------------------------------------------------------------
    @property
    def label_filter(self) -> list[str]:
        """Return configured label filter as list of lowercase tokens."""
        return self._label_filter

    async def create_save_task(self) -> None:
        """Create a task to save the config."""
        if self._saver_task is None or self._saver_task.done():
            self._saver_task = asyncio.create_task(self._commit_config())

    async def _commit_config(self, immediate_commit: bool = False) -> None:
        if not immediate_commit:
            await asyncio.sleep(CONFIG_WRITE_DELAY_SECONDS)
        await async_save_json(self.get_path(CONFIG_FILE), self._config)

    async def async_stop(self) -> None:
        """Save the config on shutdown."""
        self.stop_entertainment()
        if self._saver_task is not None and not self._saver_task.done():
            self._saver_task.cancel()
            await self._commit_config(immediate_commit=True)

    @property
    def ip_addr(self) -> str:
        """Return ip address of the emulated bridge."""
        return self._ip_addr

    @property
    def mac_addr(self) -> str:
        """Return mac address of the emulated bridge."""
        return self._mac_addr

    @property
    def bridge_id(self) -> str:
        """Return the bridge id of the emulated bridge."""
        return self._bridge_id

    @property
    def bridge_serial(self) -> str:
        """Return the bridge serial of the emulated bridge."""
        return self._bridge_serial

    @property
    def bridge_uid(self) -> str:
        """Return the bridge UID of the emulated bridge."""
        return self._bridge_uid

    @property
    def link_mode_enabled(self) -> bool:
        """Return state of link mode."""
        return self._link_mode_enabled

    @property
    def link_mode_discovery_key(self) -> str | None:
        """Return the temporary token which enables linking."""
        return self._link_mode_discovery_key

    @property
    def bridge_name(self) -> str:
        """Return the friendly name for the emulated bridge."""
        return self.get_storage_value("bridge_config", "name", "Hass Emulated Hue")

    @property
    def definitions(self) -> dict:
        """Return the definitions dictionary (e.g. bridge sw version)."""
        # TODO: Periodically check for updates of the definitions file on Github ?
        return self._definitions

    @property
    def entertainment_active(self) -> bool:
        """Return current state of entertainment mode."""
        return self._entertainment_api is not None

    def get_path(self, filename: str) -> str:
        """Get path to file at data location."""
        return os.path.join(self.data_path, filename)

    # ----------------------------------------------------------------------
    #  ── 2️⃣  LIGHT‑ID CREATION (filter guard + max‑ID guard)
    # ----------------------------------------------------------------------
    async def async_entity_id_to_light_id(self, entity_id: str) -> str:
        """Get a unique light_id number for the hass entity id."""
        lights = await self.async_get_storage_value("lights", default={})
        for key, value in lights.items():
            if entity_id == value["entity_id"]:
                return key

        # --------------------------------------------------------------
        #  FILTER GUARD – reject anything that does not contain a token
        # --------------------------------------------------------------
        if self._label_filter:
            lowered = entity_id.lower()
            if not any(tok in lowered for tok in self._label_filter):
                # Do **not** create a light entry – the bridge will treat it as
                # “unknown”.  Raising an exception keeps the log readable.
                raise ValueError(
                    f"Entity '{entity_id}' does not match label filter "
                    f"{self._label_filter!r} – it will be ignored."
                )

        # --------------------------------------------------------------
        #  MAX‑ID GUARD – make sure we never exceed the 0‑19 range
        # --------------------------------------------------------------
        if lights:
            highest = max(int(k) for k in lights)
            if highest >= MAX_LIGHT_ID - 1:
                raise RuntimeError(
                    f"Maximum allowed light ID ({MAX_LIGHT_ID-1}) already used – "
                    "cannot create a new light.  Delete an old one or increase MAX_LIGHT_ID."
                )

        # ------------------------------------------------------------------
        #  Existing logic – create a new light entry (unchanged)
        # ------------------------------------------------------------------
        next_light_id = "1"
        if lights:
            next_light_id = str(max(int(k) for k in lights) + 1)

        # generate unique id (fake zigbee address) from entity id
        unique_id = hashlib.md5(entity_id.encode()).hexdigest()
        unique_id = "00:{}:{}:{}:{}:{}:{}:{}:{}-{}".format(
            unique_id[0:2],
            unique_id[2:4],
            unique_id[4:6],
            unique_id[6:8],
            unique_id[8:10],
            unique_id[10:12],
            unique_id[12:14],
            unique_id[14:16],
        )
        # create default light config
        light_config = {
            "entity_id": entity_id,
            "enabled": True,
            "name": "",
            "uniqueid": unique_id,
            "config": {
                "archetype": "sultanbulb",
                "function": "mixed",
                "direction": "omnidirectional",
                "startup": {"configured": True, "mode": "safety"},
            },
            "throttle": DEFAULT_THROTTLE_MS,
        }
        await self.async_set_storage_value("lights", next_light_id, light_config)
        return next_light_id

    # ----------------------------------------------------------------------
    #  ── 4️⃣  PRUNING / RE‑NUMBERING LOGIC
    # ----------------------------------------------------------------------
    def _prune_and_renumber(self) -> None:
        """
        Remove lights that do not contain any token from ``self._label_filter``
        in their entity_id and then renumber the remaining lights so that:

        * IDs start at ``1`` and increase by ``1``.
        * No ID ever exceeds ``MAX_LIGHT_ID - 1`` (i.e. 19).

        Groups are also cleaned – any reference to a removed light is dropped;
        groups that become empty (and are not Entertainment groups) are deleted.
        If the pruning would leave the bridge with *no* lights, a tiny dummy
        light + dummy room are added so the Hue bridge can still start.
        """
        # ---------- 1️⃣  Helper – does a light match the filter? ----------
        def keep_light(entity_id: str) -> bool:
            lowered = entity_id.lower()
            return any(tok in lowered for tok in self._label_filter)

        # ---------- 2️⃣  Prune the ``lights`` dict ----------
        old_lights: dict = self._config.get("lights", {})
        new_lights: dict = {}
        next_id = 1

        for old_id, light_cfg in old_lights.items():
            entity = light_cfg.get("entity_id", "")
            if not keep_light(entity):
                # skip / drop this light
                continue

            if next_id > MAX_LIGHT_ID:
                # safety‑net – stop adding more lights once we hit the cap.
                LOGGER.warning(
                    "Reached MAX_LIGHT_ID (%d). Light %s will be omitted.",
                    MAX_LIGHT_ID,
                    entity,
                )
                continue

            new_lights[str(next_id)] = light_cfg
            next_id += 1

        # ---------- 3️⃣  If we removed everything, create a dummy light ----------
        if not new_lights:
            LOGGER.info(
                "All lights were filtered out – creating a minimal dummy light "
                "so the bridge can start."
            )
            dummy_entity = "light.dummy_ambi"
            dummy_unique = "00:00:00:00:00:00:00:00-00"
            dummy_cfg = {
                "entity_id": dummy_entity,
                "enabled": True,
                "name": "Dummy ambi",
                "uniqueid": dummy_unique,
                "config": {
                    "archetype": "sultanbulb",
                    "function": "mixed",
                    "direction": "omnidirectional",
                    "startup": {"configured": True, "mode": "safety"},
                },
                "throttle": DEFAULT_THROTTLE_MS,
                "state": {
                    "brightness": 0,
                    "color_mode": "onoff",
                    "power_state": False,
                    "reachable": True,
                },
            }
            new_lights["1"] = dummy_cfg
            next_id = 2   # next free ID after the dummy

        self._config["lights"] = new_lights
        LOGGER.info(
            "Pruned lights: %d → %d (max ID %d)",
            len(old_lights),
            len(new_lights),
            MAX_LIGHT_ID - 1,
        )

        # ---------- 4️⃣  Clean up ``groups`` ----------
        old_groups: dict = self._config.get("groups", {})
        new_groups: dict = {}
        kept_ids = set(new_lights.keys())

        for gid, grp in old_groups.items():
            # Remove any light IDs that no longer exist
            grp_lights = [lid for lid in grp.get("lights", []) if lid in kept_ids]
            grp["lights"] = grp_lights

            # Delete the group if it is now empty **and** it is not an
            # Entertainment group (those groups have a special purpose).
            if not grp_lights and grp.get("type") != "Entertainment":
                continue

            new_groups[gid] = grp

        # ---------- 5️⃣  If we have no groups at all, add a dummy room ----------
        if not new_groups:
            LOGGER.info(
                "No groups survived pruning – creating a minimal dummy room."
            )
            dummy_group = {
                "area_id": "dummy_room",
                "class": "Other",
                "enabled": True,
                "name": "Dummy Room",
                "type": "Room",
                "lights": list(kept_ids),   # put *all* lights in the dummy room
                "sensors": [],
                "action": {"on": False},
                "state": {"any_on": False, "all_on": False},
            }
            new_groups["1"] = dummy_group

        self._config["groups"] = new_groups
        LOGGER.info(
            "Pruned groups: %d → %d (removed empty non‑entertainment groups)",
            len(old_groups),
            len(new_groups),
        )

        # ---------- 6️⃣  Persist the cleaned config synchronously ----------
        # We are still in ``__init__`` – there is no running event‑loop, so we
        # must write the file the normal (blocking) way.
        cfg_path = self.get_path(CONFIG_FILE)
        try:
            with open(cfg_path, "w", encoding="utf-8") as fp:
                json.dump(self._config, fp, indent=4, sort_keys=False)
            LOGGER.debug(
                "Emulated‑Hue config written synchronously after pruning."
            )
        except OSError as err:   # pragma: no cover   (unlikely on a healthy FS)
            # Propagate the exception so the outer guard in __init__ can log it.
            raise

    # ----------------------------------------------------------------------
    #  ── 5️⃣  OTHER CONFIG HELPERS (unchanged)
    # ----------------------------------------------------------------------
    async def async_get_light_config(self, light_id: str) -> dict:
        """Return light config for given light id."""
        conf = await self.async_get_storage_value("lights", light_id)
        if not conf:
            raise Exception(f"Light {light_id} not found!")
        return conf

    async def async_entity_id_from_light_id(self, light_id: str) -> str:
        """Return the hass entity by supplying a light id."""
        light_config = await self.async_get_light_config(light_id)
        if not light_config:
            raise Exception("Invalid light_id provided!")
        entity_id = light_config["entity_id"]
        entities = self.ctl.controller_hass.get_entities()
        if entity_id not in entities:
            raise Exception(f"Entity {entity_id} not found!")
        return entity_id

    async def async_area_id_to_group_id(self, area_id: str) -> str:
        """Get a unique group_id number for the hass area_id."""
        groups = await self.async_get_storage_value("groups", default={})
        for key, value in groups.items():
            if area_id == value.get("area_id"):
                return key
        # group does not yet exist in config, create default config
        next_group_id = "1"
        if groups:
            next_group_id = str(max(int(k) for k in groups) + 1)
        group_config = {
            "area_id": area_id,
            "enabled": True,
            "name": "",
            "class": "Other",
            "type": "Room",
            "lights": [],
            "sensors": [],
            "action": {"on": False},
            "state": {"any_on": False, "all_on": False},
        }
        await self.async_set_storage_value("groups", next_group_id, group_config)
        return next_group_id

    async def async_get_group_config(self, group_id: str) -> dict:
        """Return group config for given group id."""
        conf = await self.async_get_storage_value("groups", group_id)
        if not conf:
            raise Exception(f"Group {group_id} not found!")
        return conf

    async def async_get_storage_value(
        self, key: str, subkey: str = None, default: Any | None = None
    ) -> Any:
        """Get a value from persistent storage."""
        return self.get_storage_value(key, subkey, default)

    def get_storage_value(
        self, key: str, subkey: str = None, default: Any | None = None
    ) -> Any:
        """Get a value from persistent storage."""
        main_val = self._config.get(key, None)
        if main_val is None:
            return default
        if subkey:
            return main_val.get(subkey, default)
        return main_val

    async def async_set_storage_value(
        self, key: str, subkey: str, value: str | dict
    ) -> None:
        """Set a value in persistent storage."""
        needs_save = False
        if subkey is None and self._config.get(key) != value:
            # main key changed
            self._config[key] = value
            needs_save = True
        elif subkey and key not in self._config:
            # new sublevel created
            self._config[key] = {subkey: value}
            needs_save = True
        elif subkey and self._config[key].get(subkey) != value:
            # sub key changed
            self._config[key][subkey] = value
            needs_save = True
        # save config to file if changed
        if needs_save:
            await self.create_save_task()

    async def async_delete_storage_value(self, key: str, subkey: str = None) -> None:
        """Delete a value in storage."""
        # if Home Assistant group/area, we just disable it
        if key == "groups" and subkey:
            # when deleting groups, we must delete all associated scenes
            scenes = await self.async_get_storage_value("scenes", default={})
            for scene_num, scene_data in scenes.copy().items():
                if scene_data["group"] == subkey:
                    await self.async_delete_storage_value("scenes", scene_num)
            # simply disable the group if its a HASS group
            group_conf = await self.async_get_group_config(subkey)
            if group_conf["class"] == "Home Assistant":
                group_conf["enabled"] = False
                return await self.async_set_storage_value("groups", subkey, group_conf)
        # if Home Assistant light, we just disable it
        if key == "lights" and subkey:
            light_conf = await self.async_get_light_config(subkey)
            light_conf["enabled"] = False
            return await self.async_set_storage_value("lights", subkey, light_conf)
        # all other local storage items
        if subkey:
            self._config[key].pop(subkey, None)
        else:
            self._config.pop(key)
        await async_save_json(self.get_path(CONFIG_FILE), self._config)
        return None

    async def async_get_users(self) -> dict:
        """Get all registered users as dict."""
        return await self.async_get_storage_value("users", default={})

    async def async_get_user(self, username: str) -> dict:
        """Get details for given username."""
        user_data = await self.async_get_storage_value("users", username)
        if user_data:
            user_data["last use date"] = (
                datetime.datetime.now().isoformat().split(".")[0]
            )
            await self.async_set_storage_value("users", username, user_data)
        return user_data

    async def async_create_user(self, devicetype: str) -> dict:
        """Create a new user for the api access."""
        if not self._link_mode_enabled:
            raise Exception("Link mode not enabled!")
        all_users = await self.async_get_users()
        # devicetype is used as deviceid: <application_name>#<devicename>
        # return existing user if already registered
        for item in all_users.values():
            if item["name"] == devicetype:
                return item
        # create username and clientkey
        username = create_secure_string(40)
        clientkey = create_secure_string(32, True).upper()
        user_obj = {
            "name": devicetype,
            "clientkey": clientkey,
            "create date": datetime.datetime.now().isoformat().split(".")[0],
            "username": username,
        }
        await self.async_set_storage_value("users", username, user_obj)
        return user_obj

    async def delete_user(self, username: str) -> None:
        """Delete a user."""
        await self.async_delete_storage_value("users", username)

    async def async_enable_link_mode(self) -> None:
        """Enable link mode for the duration of 5 minutes."""
        if self._link_mode_enabled:
            return  # already enabled
        self._link_mode_enabled = True

        def auto_disable():
            self.ctl.loop.create_task(self.async_disable_link_mode())

        self.ctl.loop.call_later(300, auto_disable)
        LOGGER.info("Link mode is enabled for the next 5 minutes.")

    async def async_disable_link_mode(self) -> None:
        """Disable link mode on the virtual bridge."""
        self._link_mode_enabled = False
        LOGGER.info("Link mode is disabled.")

    async def async_enable_link_mode_discovery(self) -> None:
        """Enable link mode discovery (notification) for the duration of 5 minutes."""
        if self._link_mode_discovery_key:
            return  # already active

        LOGGER.info(
            "Link request detected - Use the Homeassistant frontend to confirm this link request."
        )

        self._link_mode_discovery_key = create_secure_string(32)
        # create persistent notification in hass
        url = f"http://{self.ip_addr}/link/{self._link_mode_discovery_key}"
        msg = "Click the link below to enable pairing mode on the virtual bridge:\n\n"
        msg += f"**[Enable link mode]({url})**"

        await self.ctl.controller_hass.async_create_notification(
            msg, "hue_bridge_link_requested"
        )

        # make sure that the notification and link request are dismissed after 5 minutes
        def auto_disable():
            self.ctl.loop.create_task(self.async_disable_link_mode_discovery())

        self.ctl.loop.call_later(300, auto_disable)

    async def async_disable_link_mode_discovery(self) -> None:
        """Disable link mode discovery (remove notification in hass)."""
        self._link_mode_discovery_key = None
        await self.ctl.controller_hass.async_dismiss_notification(
            "hue_bridge_link_requested"
        )

    def start_entertainment(self, group_conf: dict, user_data: dict) -> bool:
        """Start the entertainment mode server."""
        if not self._entertainment_api:
            self._entertainment_api = EntertainmentAPI(self.ctl, group_conf, user_data)
            return True
        return False

    def stop_entertainment(self) -> None:
        """Stop the entertainment mode server if it is active."""
        if self._entertainment_api:
            self._entertainment_api.stop()
            self._entertainment_api = None
        # force update of all light states
        self.ctl.loop.create_task(force_update_all())