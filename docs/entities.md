# Entities

The integration creates one device per Labelito service with the following entities.

| Entity | Type | Availability | Description |
| --- | --- | --- | --- |
| Printer state | Sensor (enum) | always | `off` / `idle` / `printing` / `error`, mirroring Labelito's printer state; `status`/`phase` as attributes. |
| Loaded media | Sensor | always | The loaded roll, e.g. `62mm continuous` or `29x90mm die-cut`; width/length/type as attributes. |
| Labels printed | Sensor | always (one of two backends) | Lifetime count from the printer's SNMP counter on network printers, or a count of prints made through Home Assistant otherwise. See below. |
| Printer display | Sensor | network/SNMP only | Raw console/display text line reported by the printer. |
| Labelito version | Sensor (diagnostic) | always | Version of the Labelito service itself, from `/health`. |
| Templates | Sensor (diagnostic) | always | Number of print templates Labelito currently serves. |
| Transport | Sensor (diagnostic) | always | How Labelito talks to the printer: `network`, `usb`, or `file`. |
| Printer model | Sensor (diagnostic) | always | SNMP-reported model when reachable, else the configured `MODEL`; `model_mismatch` as an attribute. |
| Printer connectivity | Binary sensor | always | Whether the printer answered the last status query; `uri` as an attribute. |
| Printer problem | Binary sensor | always | On for reported faults or a configured-vs-reported model mismatch; details in attributes. |
| Reprint last label | Button | always | Prints the last label printed through Home Assistant again. |

Entities stay available while the *printer* is off (that is what the connectivity sensor is for);
they only become unavailable when the *Labelito service* itself is unreachable.

## Labels printed: two backends, one entity

**Labels printed** is backed by whichever counter the deployment can actually provide, decided
once when the integration is set up:

- **Network printers (SNMP)** get the printer's own lifetime marker-life counter — the true total
  ever printed, regardless of what printed it. `source: printer` in the attributes.
- **USB or file printers** have no such counter on the wire, so the integration keeps its own:
  every label printed *through this Home Assistant instance* (service call, voice command, or the
  reprint button) increments a persistent count that survives restarts. `source: home_assistant`
  in the attributes. This count cannot see labels printed any other way (e.g. directly against
  Labelito's API, bypassing Home Assistant).

Because the choice is made at setup, switching a printer's transport (network ↔ USB) changes which
backend owns this entity on the next restart or config entry reload.
