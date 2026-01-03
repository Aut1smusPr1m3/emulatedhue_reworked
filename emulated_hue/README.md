# Emulated HUE (Custom Build)

This is a custom build of the Emulated HUE add-on for Home Assistant.

Once the add-on is installed and started it will present a virtual Philips HUE
bridge on your network and expose Home Assistant lights to HUE-compatible apps.

Important notes
- The virtual bridge binds to ports 80 (HTTP) and 443 (HTTPS) on the host network.
- When running this custom build, you may want to set `EMULATED_HUE_LABEL_FILTER`
  to limit which Home Assistant entities are exposed (see the core README for details).

For configuration options and usage, see `DOCS.md`.
