# Services

## `labelito.print`

```yaml
action: labelito.print
data:
  template: pantry
  fields:
    title: "Tomato soup"
  copies: 2
```

| Field | Required | Description |
| --- | --- | --- |
| `template` | yes | labelito template name, validated at call time against the live catalog. |
| `fields` | no | Mapping of template field name → value. |
| `copies` | no | 1-10, default 1. |
| `dry_run` | no | Render and validate without printing. |
| `language` | no | Language tag for translated label chrome and dates. |
| `cut` | no | Cut after printing (server default: true). |
| `red` / `dither` | no | Two-color printing / Floyd-Steinberg dithering; unset inherits the server defaults. |
| `idempotency_key` | no | Stable key so labelito dedupes a replayed request; see retries below. |
| `config_entry_id` | no | Only needed when several labelito services are configured. |
| `seq_count` | no | Number of labels in an auto-numbering batch (1-500); see below. |
| `seq_start` | no | First sequence number (server default 1). |
| `seq_step` | no | Increment between numbers, ≥ 1 (server default 1). |
| `seq_padding` | no | Minimum zero-padded digit width, 0-32 (server default 0; `3` → `001`). |

The service supports responses: use `response_variable` to receive `{job_id, status}` where
`status` is `printed` or `dry-run` (see [`examples/print_with_response.yaml`](../examples/print_with_response.yaml)).

### Filling fields with live Home Assistant data

`fields` values are ordinary service data, so Home Assistant renders any Jinja template in them
**before** the call reaches labelito — labelito only ever receives the resolved strings. Pull in
entity state, attributes, or `now()` directly:

```yaml
action: labelito.print
data:
  template: pantry
  fields:
    title: "{{ states('sensor.kitchen_temp') }}°C"
    subtitle: "{{ now().strftime('%Y-%m-%d %H:%M') }}"
```

This is independent of labelito's own server-side tokens (`{{date}}`, `{{seq}}`, `[[translation]]`),
which labelito computes itself. The template never references HA entities; the binding lives here in
the call. To drive this from a spoken command, see
[voice-assist.md](voice-assist.md#voice-triggered-printing-with-live-data).

### Auto-numbering (`{{seq}}`)

Templates that use the `{{seq}}` token print a **numbered batch** instead of copies of one label.
Set `seq_count` to the batch size (this is what turns a print into a sequence); `seq_start`,
`seq_step`, and `seq_padding` shape the number and each inherit the server default when omitted.

```yaml
action: labelito.print
data:
  template: crate      # a template that uses {{seq}}
  fields:
    label: "Widgets"
  seq_count: 50
  seq_padding: 3       # 001, 002, … 050
```

Rules (a mismatch fails fast with a clear error):

- `seq_count` and `copies` > 1 are **mutually exclusive** — a sequence already drives the item count.
- A `{{seq}}` template **requires** a sequence, and a non-`{{seq}}` template **rejects** one
  (enforced by labelito). Check a template's `uses_seq` flag via `GET /templates` or the web UI.
- The **Labels printed** counter credits the full batch size for a sequence print. A *reprint* of a
  sequence batch credits the size recorded for that job.
- Accounting is success-only (as for multi-copy prints): if a batch fails partway through, the
  labels already printed are **not** credited and the service reports the error. Pass a stable
  `idempotency_key` when you need a retry to be safe against duplicates.

Auto-numbering is available via the service and dashboard only — not through voice/Assist.

### Retrying safely

If you omit `idempotency_key`, each call gets a fresh one — so two calls, even with identical
content, always print. That means a retry after an *ambiguous* failure (a timeout or lost response
where the label may already have been sent) can print the same label twice.

To make retries safe, pass one **stable** `idempotency_key` per logical label and reuse it on every
retry of that label. labelito recognises the repeat and prints it only once. Use a value that is
unique to the label but constant across retries, for example:

```yaml
action: labelito.print
data:
  template: pantry
  fields:
    title: "Tomato soup"
  idempotency_key: "pantry-tomato-soup-2026-07-01"
```

Errors are actionable: an unknown template lists the valid template names, and a roll mismatch
reports it in plain words (for example "The loaded roll is 62mm continuous but the template needs
29x90mm die-cut").

## `labelito.reprint_last`

Reprints the most recent label printed through Home Assistant (tracked per Home Assistant run; it
resets on restart). Mirrored by the **Reprint last label** button.
