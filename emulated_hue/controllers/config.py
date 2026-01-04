"""Hold configuration variables for the emulated hue bridge."""
import asyncio
import datetime
import hashlib
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
    matches_label_filter,          # <-- NEW IMPORT
)

from .devices import force_update_all
from .entertainment import EntertainmentAPI
from .models import Controller

LOGGER = logging.getLogger(__name__)

CONFIG_FILE = "emulated_hue.json"
DEFINITIONS_FILE = os.path.join(
    os.path.dirname(Path(__file__).parent.absolute()), "definitions.json"
)


class Config:
    """Hold configuration variables for the emulated hue bridge."""

    # ----------------------------------------------------------------------
    #  Constructor – load, parse label filter, **prune & re‑index**
    # ----------------------------------------------------------------------
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

        # --------------------------------------------------------------
        # Load persisted configuration and definitions (unchanged)
        # --------------------------------------------------------------
        self._config = load_json(self.get_path(CONFIG_FILE))
        self._definitions = load_json(DEFINITIONS_FILE)

        self._link_mode_enabled = False
        self._link_mode_discovery_key = None

        # --------------------------------------------------------------
        # IP address handling (unchanged)
        # --------------------------------------------------------------
        self._ip_addr = get_local_ip()
        LOGGER.info("Auto detected listen IP address is %s", self.ip_addr)

        # --------------------------------------------------------------
        # Port handling (unchanged)
        # --------------------------------------------------------------
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

        # --------------------------------------------------------------
        # MAC / Bridge identifiers (unchanged)
        # --------------------------------------------------------------
        mac_addr = str(get_mac_address(ip=self._ip_addr))
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

        # --------------------------------------------------------------
        # Parse label filter – hard‑coded whitelist ["wz", "spot"]
        # --------------------------------------------------------------
        try:
            env_filter = os.getenv(LABEL_FILTER_ENV_VAR, "")
        except Exception:
            env_filter = ""
        # ``parse_label_filter`` now returns the hard‑coded list
        self._label_filter: list[str] = parse_label_filter(env_filter)

        # allow persisted override from bridge_config if present
        persisted = self.get_storage_value("bridge_config", "label_filter", None)
        if persisted and isinstance(persisted, list):
            self._label_filter = [str(x).strip().lower() for x in persisted if x]

        # --------------------------------------------------------------
        # **NEW:** Clean‑up *and* **re‑index** the persisted data
        # --------------------------------------------------------------
        self._prune_and_reindex()

    # ----------------------------------------------------------------------
    #  Public helpers / properties (unchanged)
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

    # ----------------------------------------------------------------------
    #  Helper methods (unchanged)
    # ----------------------------------------------------------------------
    def get_path(self, filename: str) -> str:
        """Get path to file at data location."""
        return os.path.join(self.data_path, filename)

    async def async_entity_id_to_light_id(self, entity_id: str) -> str:
        """Get a unique light_id number for the hass entity id."""
        lights = await self.async_get_storage_value("lights", default={})
        for key, value in lights.items():
            if entity_id == value["entity_id"]:
                return key
        # light does not yet exist in config, create default config
        next_light_id = "1"
        if lights:
            next_light_id = str(max(int(k) for k in lights) + 1)
        # generate unique id (fake zigbee address) from entity id
        unique_id = hashlib.md5(entity_id.encode()).hexdigest()
        unique_id = "00:{}:{}:{}:{}:{}:{}:{}-{}".format(
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
            # sub key changed  <-- **fixed typo here**
            self._config[key][subkey] = value
            needs_save = True
        # save config to file if changed
        if needs_save:
            await self.create_save_task()

    async def async_delete_storage_value(self, key: str, subkey: str = None) -> None:
        """Delete a value in persistent storage."""
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

    # ----------------------------------------------------------------------
    #  NEW: Prune **and** re‑index the persisted data
    # ----------------------------------------------------------------------
    def _entity_matches_filter(self, entity_id: str) -> bool:
        """
        Helper that checks whether a given ``entity_id`` passes the
        hard‑coded label filter.  If the controller or HA are not fully
        initialised yet we fall back to ``True`` – this prevents accidental
        removal during very early start‑up.
        """
        try:
            hass_state = self.ctl.controller_hass.get_entity_state(entity_id)
            device_id = self.ctl.controller_hass.get_device_id_from_entity_id(entity_id)
            device_attrs = (
                self.ctl.controller_hass.get_device_attributes(device_id)
                if device_id
                else {}
            )
            return matches_label_filter(self._label_filter, device_attrs, hass_state)
        except Exception:  # pragma: no cover – defensive fallback
            return True

    def _prune_and_reindex(self) -> None:
        """
        1️⃣ **Prune** lights, groups and local items that do not match the
           whitelist (or that are malformed).

        2️⃣ **Re‑index** the numeric IDs so that after pruning we have a
           compact, gap‑free sequence (`1, 2, 3 …`).  While re‑indexing we also
           update every cross‑reference (group → lights, scenes → lightstates,
           etc.) so the configuration stays internally consistent.

        3️⃣ Write the cleaned configuration back to disk immediately.
        """
        # --------------------------------------------------------------
        # 1️⃣ Prune lights
        # --------------------------------------------------------------
        lights = self._config.get("lights", {})
        lights_to_remove = []
        for lid, lconf in lights.items():
            entity_id = lconf.get("entity_id")
            if not entity_id or not self._entity_matches_filter(entity_id):
                lights_to_remove.append(lid)

        for lid in lights_to_remove:
            LOGGER.debug("Pruning light %s (filter mismatch or malformed)", lid)
            lights.pop(lid, None)

        # --------------------------------------------------------------
        # 2️⃣ Prune groups
        # --------------------------------------------------------------
        groups = self._config.get("groups", {})
        groups_to_remove = []
        for gid, gconf in groups.items():
            # Local groups – have a ``lights`` list
            if "lights" in gconf and isinstance(gconf["lights"], list):
                new_lights = []
                for light_id in gconf["lights"]:
                    try:
                        entity_id = self.ctl.config_instance.async_entity_id_from_light_id(
                            light_id
                        )
                    except Exception:
                        # Mapping missing → drop this light reference
                        continue
                    if self._entity_matches_filter(entity_id):
                        new_lights.append(light_id)
                if new_lights:
                    gconf["lights"] = new_lights
                else:
                    # No matching lights left → drop the whole group
                    groups_to_remove.append(gid)
            # Hass‑area groups (identified by ``area_id``) are *kept*,
            # because the bridge will later filter their members at request time.
            # No action needed here.

        for gid in groups_to_remove:
            LOGGER.debug("Pruning group %s (empty after filter)", gid)
            groups.pop(gid, None)

        # --------------------------------------------------------------
        # 3️⃣ Prune local items (scenes, rules, schedules, resourcelinks)
        # --------------------------------------------------------------
        # These structures may reference lights that have just been removed.
        # If a reference points to a non‑existent light we drop the whole item.
        valid_light_ids = set(self._config.get("lights", {}).keys())

        for itemtype in ("scenes", "rules", "schedules", "resourcelinks"):
            items = self._config.get(itemtype, {})
            items_to_remove = []
            for iid, iconf in items.items():
                referenced_ids = set()
                if isinstance(iconf, dict):
                    if "lights" in iconf and isinstance(iconf["lights"], list):
                        referenced_ids.update(iconf["lights"])
                    if "lightstates" in iconf and isinstance(iconf["lightstates"], dict):
                        referenced_ids.update(iconf["lightstates"].keys())
                # If *any* referenced light still exists we keep the item.
                # Otherwise we delete it.
                if referenced_ids and not referenced_ids.intersection(valid_light_ids):
                    items_to_remove.append(iid)

            for iid in items_to_remove:
                LOGGER.debug(
                    "Pruning %s %s (no valid light references after filter)",
                    itemtype,
                    iid,
                )
                items.pop(iid, None)

        # --------------------------------------------------------------
        # 4️⃣ Re‑index **lights**
        # --------------------------------------------------------------
        # Build a mapping old_id → new_id (both strings) and rewrite
        # everything that points to a light.
        old_to_new_light: dict[str, str] = {}
        new_lights_dict: dict[str, dict] = {}
        for new_index, old_lid in enumerate(sorted(self._config.get("lights", {})), start=1):
            new_lid = str(new_index)
            old_to_new_light[old_lid] = new_lid
            new_lights_dict[new_lid] = self._config["lights"][old_lid]

        self._config["lights"] = new_lights_dict

        # --------------------------------------------------------------
        # 5️⃣ Re‑index **groups** (local groups only – area groups keep their ID)
        # --------------------------------------------------------------
        old_to_new_group: dict[str, str] = {}
        new_groups_dict: dict[str, dict] = {}
        for new_index, old_gid in enumerate(sorted(self._config.get("groups", {})), start=1):
            # If the group is a Hass area we *preserve* the original ID
            # because the API uses the area‑derived ID elsewhere.
            # Area groups always have an ``area_id`` key.
            if "area_id" in self._config["groups"][old_gid]:
                new_gid = old_gid
            else:
                new_gid = str(new_index)
                old_to_new_group[old_gid] = new_gid
            new_groups_dict[new_gid] = self._config["groups"][old_gid]

        self._config["groups"] = new_groups_dict

        # --------------------------------------------------------------
        # 6️⃣ Walk through all structures that reference lights and
        #     replace old IDs with the new ones.
        # --------------------------------------------------------------
        def _replace_light_refs(container: Any) -> Any:
            """Recursive helper – replace any light‑ID string found."""
            if isinstance(container, dict):
                new_dict = {}
                for k, v in container.items():
                    if isinstance(v, (list, dict)):
                        new_dict[k] = _replace_light_refs(v)
                    elif isinstance(v, str) and v in old_to_new_light:
                        new_dict[k] = old_to_new_light[v]
                    else:
                        new_dict[k] = v
                return new_dict
            if isinstance(container, list):
                return [_replace_light_refs(item) for item in container]
            return container

        # Update groups → lights
        for gconf in self._config.get("groups", {}).values():
            if "lights" in gconf and isinstance(gconf["lights"], list):
                gconf["lights"] = [
                    old_to_new_light.get(lid, lid) for lid in gconf["lights"]
                ]

        # Update scenes → lightstates
        for sconf in self._config.get("scenes", {}).values():
            if "lightstates" in sconf and isinstance(sconf["lightstates"], dict):
                sconf["lightstates"] = {
                    old_to_new_light.get(lid, lid): state
                    for lid, state in sconf["lightstates"].items()
                }

        # Update rules (some rule implementations store light IDs)
        for rconf in self._config.get("rules", {}).values():
            rconf = _replace_light_refs(rconf)

        # Update schedules (they may contain light IDs in payloads)
        for scheconf in self._config.get("schedules", {}).values():
            scheconf = _replace_light_refs(scheconf)

        # Update resourcelinks (rarely used, but keep consistency)
        for rlconf in self._config.get("resourcelinks", {}).values():
            rlconf = _replace_light_refs(rlconf)

        # --------------------------------------------------------------
        # 7️⃣ Finally, write the cleaned & re‑indexed configuration back
        # --------------------------------------------------------------
        # Reset any pending save task and force an immediate write.
        self._saver_task = None
        asyncio.get_event_loop().create_task(self._commit_config(immediate_commit=True))