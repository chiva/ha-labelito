# Voice (Assist)

HACS installs the integration only — Home Assistant loads custom sentences exclusively from your
config folder ([official docs][custom-sentences]), so the sentence files have to get there once.

## Setup

Call the **`labelito.write_voice_sentences`** service (Developer tools → Actions). It writes both
files, tailored to the templates you actually have, and reloads the conversation integration so
they take effect without a restart:

```text
<config>/custom_sentences/<lang>/labelito.yaml             once, then yours to edit
<config>/custom_sentences/<lang>/labelito-templates.yaml   generated, kept in step with the catalog
```

Nothing is written until you call it — installing a printer does not put files in your config
directory. After that first call the generated file is refreshed automatically whenever the
template catalog changes, so a template you add is matched by name without touching anything.
**Deleting `labelito-templates.yaml` is a complete opt-out**; voice printing keeps working through
the other file.

That file's existence *is* the record of the opt-in, which is why it is written even when the
catalog is empty — an empty list is a valid grammar whose sentences simply never match, and
skipping it would mean a service call made while labelito served nothing left the refresh
believing you had never opted in.

**Only a file carrying the generated header is ever rewritten.** If you already have a
hand-written `labelito-templates.yaml`, it is left alone and reported under `conflicts` instead —
the automatic refresh must not be able to destroy a grammar you wrote, least of all at startup
with no service call involved. The cost of keeping yours is that the opt-in can never be recorded
while it is there, so rename it if you want generated sentences too — and then run
`labelito.write_voice_sentences` again, because the automatic refresh only updates a generated
file that already exists and will not create the one you just moved out of the way.

Every list these files define is prefixed with `labelito_` (`labelito_template_name`,
`labelito_template`, `labelito_text`), and hassil's `{list:slot}` form maps each back onto the
plain slot the intent expects. Home Assistant merges every custom-sentence file for a language
into one dictionary, so `lists` is a shared namespace: with a generic name, another grammar's
values merge into ours. Measured — a second file defining its own `template_name` made *"imprime
una etiqueta de tele"* resolve to `television`, a value with nothing to do with labels.

Nothing about this is allowed to stop the printer working. The refresh runs at setup and off the
status poll, and a failure in it — an unwritable config directory, a conversation integration that
will not reload, a template catalog whose shape is not what this integration expects — is logged
and dropped. A service call you made yourself still reports its errors to you.

The service optionally returns a response naming what it wrote, how many spoken forms the list
holds, and any name it had to leave out (see [Two grammars](#two-grammars-and-why-both)).

### Doing it by hand instead

If you would rather not have the integration write to your config directory, download the
[`custom_sentences/`](../custom_sentences) folder from this repository into `<config>` so the files
land at `<config>/custom_sentences/{en,es}/labelito.yaml`. That is the same wildcard grammar the
service writes — you just do not get the exact-match list.

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

The spoken template name is matched against the live catalog, the free-form text fills the
template's first required field, and the reply — and the printed label's language — follow the
language you spoke in.

## Aliases: the other way you say a name

A template can declare alternative **spoken** names, in its own YAML:

```yaml
name: meal-prep
aliases: [comida preparada, batch cooking]
```

Two reasons that helps. A saved name is not always a spoken one — `meal-prep` is never said with
the hyphen (the integration derives "meal prep" on its own, so that alias would be redundant) — and
the same thing often has another word: a Spanish household says *congelado* about as often as
*congelador*.

Aliases are never a lookup key: printing is always by `name`, and the reply names the canonical
template. A template's own name always outranks another template's alias, so an alias can never
resolve in place of a real name. A spoken form that **two** templates claim resolves to neither —
there is no way to tell which was meant — and `write_voice_sentences` reports it, since the only
fix is renaming one of them. See
[Aliases (spoken names)](https://github.com/chiva/labelito/blob/main/docs/template-format.md#aliases-spoken-names)
in labelito's template reference.

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

### Dry-running voice prints

By default, every successful voice match puts a label on the tape, which makes iterating on
sentences or template names expensive. The integration's **Options** (gear icon on the config
entry) carry a **Dry-run voice prints** toggle: while it is on, prints from the built-in
`LabelitoPrint` intent are sent to labelito with `dry_run`, so the label is rendered and validated — a template miss, a missing required field or a
media mismatch is reported exactly as it would be for a real print — but nothing is printed. The
spoken reply leads with "Dry run: nothing printed" so a dry run can't be mistaken for a printer
that quietly failed.

**The toggle covers the built-in `LabelitoPrint` intent only** — option 1 above. Option 2 is
still voice as far as you are concerned, but a `conversation` trigger calls the `labelito.print`
service, which has its own `dry_run` field and never reads this option: those automations keep
printing for real while you debug sentences. Worth knowing before you assume nothing can reach the
tape.

### Limitations

- **The `LabelitoPrint` intent's text is literal.** Saying "print the temperature" prints those
  words, not a sensor value. For live data, use option 2.
- **One free-text field** via the intent — the spoken text fills only the template's *first*
  required field. Multiple fields need a service call (option 2).
- **LLM default agent:** if your default conversation agent is an LLM (OpenAI/Gemini), enable
  "prefer handling commands locally" so exact-sentence triggers and the built-in intent fire before
  the LLM takes over.
- **Auto-numbering (`{{seq}}`) is not available by voice** — use the service or dashboard.
- **A mis-heard name is resolved by similarity, not exactly.** "cogelador" reaches `congelador`
  through the fuzzy matcher, which is a guess — a good one, but a template whose name is one letter
  from another's can be mistaken. Exact matching applies only to names in the generated list.
- **Spanish free-text falls back to handler-side recovery** (see below) for any name the generated
  list does not cover — a mis-heard one, or one added since the last refresh. English never needed
  the recovery.
- **A template name containing a connector word** (*para* / *for* / *que diga* / *that says*)
  loses its tail to the dictated text *without* the generated list. Spoken alone it is fine — the
  handler matches a full name exactly before it tries splitting — but *"haz una etiqueta de regalo
  para navidad para juan"* arrives as one slot that matches no name exactly, so recovery splits at
  the **first** connector and resolves `regalo` with the text "navidad para juan". With the list
  both parse correctly: the two candidate parses bind one wildcard each and match the same
  literals, so hassil's third criterion decides — less text captured by the wildcard — which is
  `regalo-para-navidad` plus "juan". `labelito.write_voice_sentences` is the fix for such a name.

## Two grammars, and why both

Two sentence files are in play, and each is wrong on its own:

| | `labelito.yaml` | `labelito-templates.yaml` |
|---|---|---|
| the `template` slot is | a **wildcard** — anything | a **closed list** of the names you have |
| written | once, if missing; then yours | regenerated when the catalog changes |
| gets right | a name nobody enumerated: one added a minute ago, or one the speech-to-text engine mangled | the text boundary, multi-word names, punctuation and casing, connector words in a name |
| gets wrong | folds the whole Spanish utterance into one slot (below) | stale the moment a template is added; can never cover a mis-heard name |

Home Assistant's default agent resolves sentences with
[`recognize_best`](https://github.com/home-assistant/hassil), which scores every parse and prefers,
in order, **fewer wildcards** and then **more literal text matched**. Both preferences point the
same way here, so no tie-breaking trickery is needed: for *"imprime una etiqueta de congelador para
lasaña"* the closed-list parse binds one wildcard (`text`) and matches one literal more (*para*)
than the wildcard parse, so it wins and `text` comes out clean. An utterance the list cannot match
has no closed-list parse at all, falls to the wildcard sentences, and is recovered by the handler.
**A stale or missing list therefore degrades, never breaks** — which is what makes regenerating it
safe to do automatically.

Two names never make it into the list, and both simply fall back to the wildcard:

* a spoken form **more than one template claims** — `pantry-1` and `pantry_1` are both said "pantry
  one" — because printing the wrong one is worse than saying "I don't know that template" and
  reading out the real list;
* a spoken form carrying **sentence-grammar syntax** (`(`, `)`, `[`, `]`, `{`, `}`, `<`, `>`,
  `|`, `;`, `\`), which hassil would parse instead of match — and for `{...}` would refuse to
  recognize *anything* in that language with. Both a name and an alias can carry these: labelito
  only constrains the names it saves itself, so a YAML file dropped into its templates directory
  by hand can be named anything, and an alias arrives over HTTP from a service this integration
  does not own.

`write_voice_sentences` reports both in its response, because in both cases the fix is a rename and
nothing else would tell you.

Anything read from the catalog is treated as untrusted for the same reason. An `aliases` value
that is not a list of strings contributes nothing: a bare string would otherwise register one
spoken form per *character*, and a single letter is a substring of almost any utterance, so the
fuzzy matcher would resolve it and print the wrong label.

The generated file is replaced atomically — staged in the same directory, then moved into place —
so a failed write leaves the previous grammar intact instead of a truncated one Home Assistant
would try to load. A truncated closed-list file is the dangerous case, not an ignored one: it can
still parse as a mapping whose sentences reference the list its cut-off tail defined.

### Why the list ships in the same file as its sentences

hassil raises `MissingListError` when a sentence references a list that is not defined — and it
raises it while *recognizing*, which discards every result for that language. Putting the
closed-list sentences in one file and the generated list in another would therefore take down all
voice control for that language whenever the list was missing, wildcard fallback included. So each
file defines exactly the lists its own sentences use and is a complete grammar on its own; either
can be deleted without touching the other. `tests/test_voice_sentences.py` pins both halves of
that, including the `MissingListError` behaviour itself — so if a future hassil stops discarding
everything, the split stops being necessary and we find out from a failing test rather than
carrying the complexity on a stale assumption.

## How the text is extracted without the list (and why Spanish needs help)

With `template` and `text` both **wildcard** lists — the only grammar available before the list is
generated, and the one every unmatched utterance falls back to — a sentence ending in a trailing
`{template}` wildcard with no literal after it makes `recognize_best` fold the *entire* utterance
into `template`:

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

**When the name before the connector isn't a template**, the utterance is reported as a miss and
you hear the unknown-template prompt with the real template list. It is deliberately *not* matched
as a whole: the fuzzy matcher's substring rule would then happily return a template merely *named
inside the dictated text* — *"haz una etiqueta de nevera para queso manchego"* with no `nevera`
template would print a `queso` label with no text at all — which is a wrong label produced
silently. Only when no connector is present at all does the whole utterance go to the fuzzy
matcher, where that substring rule is the intended rescue for ASR noise
(*"freezer dated uno dos tres"* → `freezer-dated`).

**The same applies when there is no name at all.** Because *de* is optional, *"haz una etiqueta
para queso manchego"* collapses to `template="para queso manchego"` — a slot that opens with a
connector. There is nothing before the boundary to resolve, so it is the same miss, as is a slot
that is only punctuation (*"."*, or a *", para …"* your speech-to-text engine punctuated) since
none of that names a template either. Worth knowing because the first is the shape you get from a
perfectly natural sentence that simply omits the template: you will hear the available templates
rather than a label you did not ask for.

**Punctuation is normalized away.** Streaming speech-to-text engines emit mixed-case, punctuated
text, and the no-text sentence puts `{template}` last — so the template name reliably arrives as
*"pantry."*. `_normalize` strips sentence punctuation from both sides of every comparison, which
also means a punctuated connector (*"que diga,"*) still marks the template/text boundary instead
of silently discarding the dictated text.

`tests/test_intents.py` locks this down, including a `recognize_best` regression test over the
shipped YAML so the behavior can be re-validated if the sentence files change. Those tests run
against the real hassil (pinned in `requirements_test.txt` — it is not a dependency of the Home
Assistant wheel, so without the pin they would silently skip).

## Reference

- Home Assistant — [Custom sentences (YAML): file layout, structure, and customizing responses][custom-sentences].

[custom-sentences]: https://www.home-assistant.io/voice_control/custom_sentences_yaml/#customizing-responses
