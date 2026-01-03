# Emulated HUE for Home Assistant (custom)

Configuration options available for this add-on:

- `http_port` (int): custom HTTP port (defaults to 80)
- `https_port` (int): custom HTTPS port (defaults to 443)
- `use_default_ports_for_discovery` (bool): whether to use default ports for discovery
- `verbose` (bool): enable verbose logging

Environment variables
- `HASS_URL`: URL of your Home Assistant instance
- `HASS_TOKEN`: long-lived access token
- `EMULATED_HUE_LABEL_FILTER`: optional comma-separated tokens to restrict exposed lights

Run notes
- The add-on requires host networking for discovery to work reliably.
