# Labelito for Home Assistant

A Home Assistant custom integration for [Labelito](https://github.com/chiva/labelito), the
self-hosted label printing service for Brother QL printers. Print labels from automations, scripts,
dashboards, and by voice through Assist.

- **Printer entities** — state, loaded roll, connectivity, faults, and lifetime label count.
- **`labelito.print` service** — print any template with field values, validated against the live
  catalog before anything reaches the printer.
- **`labelito.reprint_last`** — service and button to print the last label again.
- **Voice** — "print a pantry label for tomato soup" via Assist custom sentences.
- **Add-on discovery** — one-click setup when Labelito runs as the
  [addon-labelito](https://github.com/chiva/addon-labelito) add-on.

## The Labelito ecosystem

Labelito is three pieces. This repo is the Home Assistant **integration** — it does not run the
printer service itself; it talks to a Labelito service you run separately (or as the add-on).

| Project | What it is | Use it when |
| --- | --- | --- |
| [`labelito`](https://github.com/chiva/labelito) | The label-printing **service** (the engine). | You want to run it anywhere with Docker. |
| [`addon-labelito`](https://github.com/chiva/addon-labelito) | Packages that service as a **Home Assistant add-on**. | You run Home Assistant OS/Supervised. |
| **`ha-labelito`** (this repo) | HACS **integration** — entities, services, voice. | You want to print from automations, dashboards, or Assist. |

## Requirements

- A running Labelito service (Docker, bare metal, or the Home Assistant add-on) reachable from
  Home Assistant, speaking Labelito API version 1.
- Home Assistant 2026.7 or newer (it ships Python 3.14, which this integration requires).

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chiva&repository=ha-labelito&category=integration)

The integration is not (yet) in the HACS default store, so add it as a custom repository:

1. Click the badge above, **or** open **HACS → ⋮ → Custom repositories**, paste
   `https://github.com/chiva/ha-labelito`, and pick type **Integration**.
2. Search HACS for **Labelito**, open it, and press **Download**.
3. Restart Home Assistant.

Manual alternative: copy `custom_components/labelito/` into `<config>/custom_components/` and
restart.

## Configuration

1. Go to **Settings → Devices & services → Add integration** and search for **Labelito**.
2. Enter the **host**, **port** (default `8765`), and the **API token** (leave empty only if
   Labelito runs with `ALLOW_UNAUTHENTICATED=true`).
3. Submit. The flow verifies reachability, checks the API version, and validates the token.

If Labelito runs as the Home Assistant add-on, the integration is discovered automatically and
setup is a single confirmation click.

**Options** (gear icon): override the printer status poll interval (default 30 s for network
printers, 90 s for USB).

## Usage

```yaml
action: labelito.print
data:
  template: pantry
  fields:
    title: "Tomato soup"
  copies: 2
```

See the reference docs for the full surface:

- **[docs/services.md](docs/services.md)** — `labelito.print` / `labelito.reprint_last` fields,
  responses, and error behavior.
- **[docs/entities.md](docs/entities.md)** — every sensor/binary sensor/button, and how the
  "Labels printed" counter is sourced.
- **[docs/voice-assist.md](docs/voice-assist.md)** — installing the voice sentences for Assist.

Ready-to-adapt automations live in [`examples/`](examples/).

## License

This integration is licensed under the [MIT License](LICENSE). Labelito itself is GPL-3.0: the
integration talks to it purely over its HTTP API and vendors none of its code, so the licenses
remain independent.
