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

The service supports responses: use `response_variable` to receive `{job_id, status}` where
`status` is `printed` or `dry-run` (see [`examples/print_with_response.yaml`](../examples/print_with_response.yaml)).

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
