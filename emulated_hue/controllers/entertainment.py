"""Experimental support for Hue Entertainment API."""
# https://developers.meethue.com/develop/hue-entertainment/philips-hue-entertainment-api/
import asyncio
import logging
import os

from emulated_hue.controllers.devices import async_get_device
from .models import Controller

LOGGER = logging.getLogger(__name__)

COLOR_TYPE_RGB = "RGB"
COLOR_TYPE_XY_BR = "XY Brightness"
HASS_SENSOR = "binary_sensor.emulated_hue_entertainment_active"


# ----------------------------------------------------------------------
#  Locate an OpenSSL binary that can act as a DTLS server.
# ----------------------------------------------------------------------
if os.path.isfile("/usr/local/opt/openssl@1.1/bin/openssl"):
    OPENSSL_BIN = "/usr/local/opt/openssl@1.1/bin/openssl"
elif os.path.isfile("C:/Program Files/Git/usr/bin/openssl.exe"):
    OPENSSL_BIN = "C:/Program Files/Git/usr/bin/openssl.exe"
else:
    OPENSSL_BIN = "openssl"


def chunked(size, source):
    """Yield successive chunks of ``size`` bytes from ``source``."""
    for i in range(0, len(source), size):
        yield source[i: i + size]


class EntertainmentAPI:
    """Handle UDP socket for HUE Entertainment (streaming mode)."""

    def __init__(self, ctl: Controller, group_details: dict, user_details: dict):
        """Initialize the class."""
        self.ctl: Controller = ctl
        self.group_details = group_details
        self._interrupted = False
        self._socket_daemon = None
        self._timestamps = {}
        self._prev_data = {}
        self._user_details = user_details
        # start the background task that reads the DTLS stream
        self.ctl.loop.create_task(self.async_run())

        # --------------------------------------------------------------
        #  Packet layout constants (taken from the Hue spec)
        # --------------------------------------------------------------
        self._pkt_header_begin_size = 9      # “HueStream”
        self._pkt_header_protocol_size = 7   # protocol version, sequence, etc.
        self._pkt_header_uuid_size = 36
        self._pkt_light_data_size = 9 * 20   # max 20 channels, 9 bytes each
        self._max_pkt_size = (
            self._pkt_header_begin_size
            + self._pkt_header_protocol_size
            + self._pkt_header_uuid_size
            + self._pkt_light_data_size
        )

        num_lights = len(self.group_details["lights"])
        # typical packet size for the current group (will be adjusted on‑the‑fly)
        self._likely_pktsize = 16 + (9 * num_lights)

    # ----------------------------------------------------------------------
    #  Main loop – reads DTLS packets from the OpenSSL subprocess
    # ----------------------------------------------------------------------
    async def async_run(self):
        """Run the DTLS server and process incoming Hue‑Entertainment packets."""
        LOGGER.info("Start HUE Entertainment Service on UDP port 2100.")
        await self.ctl.controller_hass.set_state(
            HASS_SENSOR,
            "on",
            {"room": self.group_details["name"]},
        )

        args = [
            OPENSSL_BIN,
            "s_server",
            "-dtls",
            "-accept",
            "2100",
            "-nocert",
            "-psk_identity",
            self._user_details["username"],
            "-psk",
            self._user_details["clientkey"],
            "-quiet",
        ]

        # NOTE: ``stdin`` must be kept open for OpenSSL even though we never use it.
        self._socket_daemon = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            limit=self._max_pkt_size,
        )

        buffer = b""
        while not self._interrupted:
            # --------------------------------------------------------------
            #  Keep the buffer from growing without bound – we only need
            #  at most two full packets (header + payload) at any time.
            # --------------------------------------------------------------
            buffer = buffer[
                -((self._max_pkt_size + self._pkt_header_begin_size) * 2) :
            ]

            # read the next chunk; the size guess is updated after each packet
            buffer += await self._socket_daemon.stdout.read(self._likely_pktsize)

            # packets are delimited by the literal string “HueStream”
            pkts = buffer.split(b"HueStream")
            if len(pkts) > 1:
                pkt = None
                for pkt in pkts[:-1]:
                    pkt = b"HueStream" + pkt
                    await self.__process_packet(pkt)
                buffer = pkts[-1]

                # adjust the next read size to match the real packet length
                if (guess := len(pkt) - len(buffer)) > 0:
                    self._likely_pktsize = guess

    # ----------------------------------------------------------------------
    #  Graceful shutdown
    # ----------------------------------------------------------------------
    def stop(self):
        """Stop the Entertainment service."""
        self._interrupted = True
        if self._socket_daemon:
            self._socket_daemon.kill()
        self.ctl.loop.create_task(
            self.ctl.controller_hass.set_state(HASS_SENSOR, "off")
        )
        LOGGER.info("HUE Entertainment Service stopped.")

    # ----------------------------------------------------------------------
    #  Packet handling
    # ----------------------------------------------------------------------
    async def __process_packet(self, packet: bytes) -> None:
        """Validate the packet header and dispatch per‑light data."""
        # ignore any packet that does not contain the minimal header
        if len(packet) < self._pkt_header_begin_size + self._pkt_header_protocol_size:
            return

        version = packet[9]
        # colour space: 0 → RGB, anything else → XY‑Brightness
        color_space = COLOR_TYPE_RGB if packet[14] == 0 else COLOR_TYPE_XY_BR

        # version 1: payload starts at byte 16, version 2: at byte 52
        lights_data = packet[16:] if version == 1 else packet[52:]

        tasks = []
        for light_data in chunked(9, lights_data):
            tasks.append(self.__async_process_light_packet(light_data, color_space))
        await asyncio.gather(*tasks)

    async def __async_process_light_packet(self, light_data, color_space):
        """Translate a single 9‑byte channel into Home Assistant service calls."""
        # --------------------------------------------------------------
        #  Correctly decode the 16‑bit light identifier (big‑endian)
        # --------------------------------------------------------------
        light_id = str((light_data[1] << 8) | light_data[2])

        # Retrieve the light configuration (entity_id, etc.) from the bridge config
        light_conf = await self.ctl.config_instance.async_get_light_config(light_id)

        # ------------------------------------------------------------------
        #  Build a control state object and populate it with the streamed data
        # ------------------------------------------------------------------
        entity_id = light_conf["entity_id"]
        device = await async_get_device(self.ctl, entity_id)
        call = device.new_control_state()
        call.set_power_state(True)

        if color_space == COLOR_TYPE_RGB:
            # Each colour component is a 16‑bit value; dividing by 256 brings it
            # into the 0‑255 range expected by Home Assistant.
            red   = int((light_data[3] * 256 + light_data[4]) / 256)
            green = int((light_data[5] * 256 + light_data[6]) / 256)
            blue  = int((light_data[7] * 256 + light_data[8]) / 256)

            call.set_rgb(red, green, blue)
            # Approximate brightness as the average of the three channels.
            call.set_brightness(int(sum(call.control_state.rgb_color) / 3))
        else:   # XY‑Brightness mode
            # Convert the 16‑bit XY values into the 0‑1 float range HA expects.
            x = float((light_data[3] * 256 + light_data[4]) / 65535)
            y = float((light_data[5] * 256 + light_data[6]) / 65535)

            call.set_xy(x, y)
            # Brightness is the last two bytes (0‑65535 → 0‑255 after division).
            call.set_brightness(int((light_data[7] * 256 + light_data[8]) / 256))

        # No transition – the light should follow the stream instantly.
        call.set_transition_ms(0, respect_throttle=True)
        await call.async_execute()