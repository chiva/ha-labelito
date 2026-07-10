# Voice (Assist)

HACS installs the integration only — Home Assistant loads custom sentences exclusively from your
config folder ([official docs][custom-sentences]), so the sentence files have to be added by hand
once. Download the [`custom_sentences/`](../custom_sentences) folder from this repository and drop
it into your `<config>` directory so the files land at:

```text
<config>/custom_sentences/en/labelito.yaml
<config>/custom_sentences/es/labelito.yaml
```

On **Core** or **Container** installs, run this from the folder where you downloaded the repo's
`custom_sentences/` directory, setting `CONFIG` to your Home Assistant config directory:

```bash
CONFIG=/path/to/home-assistant/config
mkdir -p "$CONFIG"/custom_sentences/en "$CONFIG"/custom_sentences/es
cp custom_sentences/en/labelito.yaml "$CONFIG"/custom_sentences/en/
cp custom_sentences/es/labelito.yaml "$CONFIG"/custom_sentences/es/
```

On **Home Assistant OS / Supervised**, if you don't have shell access (e.g. via the Terminal & SSH
add-on), use the **File editor**, **Samba**, or **Studio Code Server** add-on to create
`custom_sentences/<lang>/` under `/config` and upload the two files.

Reload Home Assistant (or restart), then say things like:

- "print a pantry label for tomato soup"
- "make a freezer-dated label that says lasagna"
- "imprime una etiqueta de pantry para sopa de tomate"

The spoken template name is fuzzy-matched against the live catalog, the free-form text fills the
template's first required field, and the reply — and the printed label's language — follow the
language you spoke in.

## Two ways to print by voice

**1. Speak the label contents** — the built-in `LabelitoPrint` intent (the examples above). Best for
ad-hoc labels whose text you dictate out loud.

**2. Use a fixed voice command as a trigger** — best when the label should carry **live Home
Assistant data** (a sensor value, a date, an attribute). You define a set phrase that runs the
`labelito.print` service, and Home Assistant renders the templated fields against current state at
the moment you speak. This also sidesteps the free-text parsing limits of option 1.

### Voice-triggered printing with live data

A `conversation` trigger needs no custom-sentence files — put the phrases inline in an automation:

```yaml
automation:
  - alias: "Print kitchen temp label by voice"
    triggers:
      - trigger: conversation
        command:
          - "print the kitchen temperature label"
          - "imprime la etiqueta de temperatura de la cocina"
    actions:
      - action: labelito.print
        data:
          template: pantry
          fields:
            title: "Cocina {{ states('sensor.kitchen_temp') }}°C  {{ now().strftime('%H:%M') }}"
```

To capture part of what you say and combine it with HA data, add a **single trailing wildcard** slot
and read it back through `trigger.slots`:

```yaml
    triggers:
      - trigger: conversation
        command:
          - "print a label that says {text}"
    actions:
      - action: labelito.print
        data:
          template: pantry
          fields:
            title: "{{ trigger.slots.text }} — {{ states('sensor.kitchen_temp') }}°C"
```

`{{ trigger.slots.text }}` is the captured slot; `{{ trigger.sentence }}` is the whole utterance.
Use a *single* trailing wildcard — two competing wildcards cause the matching issue described below.

`intent_script` (in `configuration.yaml`) is an alternative with a built-in `speech:` block for a
spoken confirmation; the `conversation` trigger is simpler and lives entirely in an automation.

### Where the data lives: templates vs. service calls

labelito templates **do not** reference Home Assistant entities — a template only declares fields
(`title`, `subtitle`, …) plus its own server-side tokens (`{{date}}`, `{{seq}}`, `[[translation]]`).
The entity reference belongs in the **service call**, which Home Assistant renders *before* sending:

```yaml
fields:
  title: "{{ states('sensor.kitchen_temp') }}"   # HA renders this to a value; labelito sees a string
```

So any existing template works with live data as long as it defines the field you target — you never
embed an entity name in the template itself.

### Limitations

- **The `LabelitoPrint` intent's text is literal.** Saying "print the temperature" prints those
  words, not a sensor value. For live data, use option 2.
- **One free-text field** via the intent — the spoken text fills only the template's *first*
  required field. Multiple fields need a service call (option 2).
- **LLM default agent:** if your default conversation agent is an LLM (OpenAI/Gemini), enable
  "prefer handling commands locally" so exact-sentence triggers and the built-in intent fire before
  the LLM takes over.
- **Auto-numbering (`{{seq}}`) is not available by voice** — use the service or dashboard.
- **Spanish free-text** relies on handler-side recovery (see below); English does not.
- **Template names should not contain connector words** (*para* / *for* / *que diga* / *that says*)
  if you want free text with them. An exactly-spoken name always resolves (even one with a
  connector), but recovery treats the first connector as the template/text boundary — so a template
  named `regalo-para-navidad` used *with* dictated text can't be told apart from *"regalo para
  &lt;text&gt;"* (template `regalo` + text).

## How the text is extracted (and why Spanish needs help)

`template` and `text` are both **wildcard** lists, and Home Assistant's default agent resolves
sentences with [`recognize_best`](https://github.com/home-assistant/hassil). When a sentence ends
in a trailing `{template}` wildcard with no literal after it, `recognize_best` prefers to fold the
*entire* utterance into `template`:

- English is safe because the required word **"label" sits after `{template}`**
  (`print [a] {template} label [for] {text}`), so the wildcard can only capture the template name
  and `text` is extracted cleanly.
- Spanish has no natural trailing anchor — the noun *etiqueta* comes **before** the template
  (`imprime una etiqueta [de] {template}`), so the no-text sentence swallows the whole phrase and
  `text` is never set. Spoken *"imprime una etiqueta de pantry para sopa de tomate"* arrives as
  `template="pantry para sopa de tomate"`.

The connectors *para* / *que diga* can't fix this at the grammar layer: they only appear in the
with-text sentence, and any artificial trailing anchor added to the no-text sentence would make it
mandatory (breaking the bare *"imprime una etiqueta de pantry"*). So the recovery lives in the
handler: `_split_template_and_text` (in `intents.py`) splits the `template` slot at the **first
connector phrase** (`CONNECTOR_PHRASES`) — everything before it is the template name (matched
exactly or fuzzily, so ASR variants like *"pantri"* still resolve), everything after is the spoken
text. This relies on the assumption that **template names contain no connector words** (see the
limitation below). If a required-field template still ends up with no text, labelito rejects it with
a `missing_required` 422 (labelito stays authoritative, so a stale cached catalog can't wrongly veto
a print), and the handler turns that into an actionable spoken prompt instead of the raw error.

`tests/test_intents.py` locks this down, including a `recognize_best` regression test over the
shipped YAML so the behavior can be re-validated if the sentence files change.

## Reference

- Home Assistant — [Custom sentences (YAML): file layout, structure, and customizing responses][custom-sentences].

[custom-sentences]: https://www.home-assistant.io/voice_control/custom_sentences_yaml/#customizing-responses
