# Voice (Assist)

Integrations cannot bundle custom sentences, so copy the shipped sentence files into your config
once:

```bash
mkdir -p config/custom_sentences/en config/custom_sentences/es
cp sentences/en/labelito.yaml config/custom_sentences/en/
cp sentences/es/labelito.yaml config/custom_sentences/es/
```

Reload Home Assistant (or restart), then say things like:

- "print a pantry label for tomato soup"
- "make a freezer-dated label that says lasagna"
- "imprime una etiqueta de pantry para sopa de tomate"

The spoken template name is fuzzy-matched against the live catalog, the free-form text fills the
template's first required field, and the reply — and the printed label's language — follow the
language you spoke in.
