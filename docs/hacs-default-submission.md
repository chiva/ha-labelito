# Getting labelito into the HACS default store

Goal: list `chiva/ha-labelito` in the [HACS default store](https://www.hacs.xyz/docs/publish/start/)
so users can install it without adding a custom repository. The steps below cover the prerequisites
that live **outside** this repo — they can only be done by the repo owner (or a major contributor)
from a **personal** GitHub account.

References: [Publishing — start](https://www.hacs.xyz/docs/publish/start/) ·
[Include requirements](https://www.hacs.xyz/docs/publish/include/) ·
[Integration requirements](https://www.hacs.xyz/docs/publish/integration/)

## Already satisfied (no action needed)

Verified on this repo:

- Public GitHub repo, not archived, not a fork, issues enabled.
- Repository **description** set and **topics** defined.
- MIT `LICENSE`.
- `hacs.json` present with a `name` field.
- Valid `custom_components/labelito/manifest.json` with all required keys: `domain`,
  `documentation`, `issue_tracker`, `codeowners`, `name`, `version`.
- CI checks exist and are visible as their own badges: **Hassfest**
  (`.github/workflows/hassfest.yml`) and **HACS** (`.github/workflows/hacs.yml`).

## Checklist

### 1. Register brand assets in home-assistant/brands — **blocker, do first**

`labelito` is **not** yet in [home-assistant/brands](https://github.com/home-assistant/brands)
(the `custom_integrations/labelito` path returns 404). The `custom_components/labelito/brand/*.png`
files in **this** repo do **not** satisfy the requirement — brand assets must live in the brands
repo.

- Fork `home-assistant/brands`.
- Add `custom_integrations/labelito/icon.png` (256×256) and `icon@2x.png` (512×512). Optionally add
  `logo.png` / `logo@2x.png`. Source PNGs already exist at `custom_components/labelito/brand/` — reuse
  or re-export them at the required sizes.
- Open a PR to `home-assistant/brands` and get it **merged before** submitting to hacs/default.

### 2. Make the Hassfest and HACS checks green on `main`

Confirm both workflows pass on the default branch after merging the workflow split.

### 3. Cut a real GitHub release

HACS requires a full **release**, not just a tag. `release-please` is already wired
(`.github/workflows/release-please.yml`) — merge the open release PR it creates to publish the
release.

### 4. Submit the hacs/default PR

- From a **personal** account (not an org), fork [hacs/default](https://github.com/hacs/default).
- Branch off `master`.
- Add `chiva/ha-labelito` **alphabetically** to the `integration` file.
- Follow the PR template exactly. Only the owner / a major contributor may submit.

### 5. After the hacs/default PR merges

Update `README.md`:

- Swap the `HACS Custom` shield for a `HACS Default` one.
- Update the **Installation** section to drop the "add as a custom repository" instructions — the
  integration is now searchable directly in HACS.
