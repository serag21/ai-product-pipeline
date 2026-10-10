# AI Product Pipeline

Local-first automation for AI-assisted digital products, starting with printable coloring products.

## Current milestone

The generation foundation is proven:

**ComfyUI -> local API -> deterministic Python product worker -> validated PNG pages**

The repository contains the proven FLUX.2 Dev workflow at `workflows/flux2_coloring_api.json`.

## Product generation

The generator is now product-manifest driven. Each product lives under:

`products/<product-id>/prompts.json`

The manifest can use either the original text prompt format or the FLUX.2 structured JSON prompt format. Structured JSON is the preferred format for production books because it gives every page the same schema while allowing scenes, subjects, composition, mood, and other elements to vary independently.

The current prototype uses a shared visual bible across all five pages:

`products/little-worlds-cozy-corners-5-prototype/prompts.json`

This is deliberately a visual-consistency gate, not the final sellable book. The five pages should read as one cohesive coloring-book series before we scale to a larger catalog product.

Run the prototype:

```powershell
.\.venv\Scripts\python.exe scripts\generate_product.py --product products/little-worlds-cozy-corners-5-prototype --count 5
```

Create its visual QA sheet:

```powershell
.\.venv\Scripts\python.exe scripts\contact_sheet.py --product products/little-worlds-cozy-corners-5-prototype
```

Generated prototype pages and QA artifacts stay under the product directory and are ignored by git.

The original construction-vehicle manifest remains available as an engineering regression test:

```powershell
.\.venv\Scripts\python.exe scripts\generate_product.py --product products/construction-vehicles-toddler-30 --count 5
```

## FLUX.2 prompting strategy

The product manifests follow the current Black Forest Labs prompting guidance:

- Main scene and subjects are placed first.
- Each subject has a clear description, position, and action.
- Style is defined once in a reusable visual bible and repeated across pages.
- Structured JSON is used for the automated production workflow.
- The coloring-book visual language is expressed positively as desired output instead of relying on negative prompts.
- Seeds remain deterministic so a page can be reproduced.

See the official guide:
https://docs.bfl.ai/guides/prompting_guide_flux2


## Reference-conditioned visual refinement

The selected page-35 composition can now be used as a visual reference with FLUX.2 Dev's `ReferenceLatent` conditioning path. The input image guides the broad composition while the prompt asks FLUX.2 to redesign the scene.

The focused follow-up experiment is:

`products/little-worlds-enchanted-nook-reference-lab-12/prompts.json`

It holds the magical tree-library scene and reference image consistent while varying prompt wording and structure. This lab deliberately avoids a lighting field: the desired coloring-page treatment is defined through black contour lines, blank white interiors, sparse interior lines, and broad open regions.

Run it from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\generate_product.py --product products/little-worlds-enchanted-nook-reference-lab-12 --reference products/little-worlds-prompt-lab-36/pages/page_035.png --count 12
.\.venv\Scripts\python.exe scripts\contact_sheet.py --product products/little-worlds-enchanted-nook-reference-lab-12
```

The generator uploads the reference image to the local ComfyUI input folder, then uses the separate reference-conditioned API workflow. The regular text-to-image path remains unchanged when `--reference` is omitted.

## Design principles

- Reuse existing tools, APIs, and open-source implementations before writing custom infrastructure.
- Keep LLMs out of the long-running GPU generation loop.
- Use deterministic local Python orchestration for production jobs.
- Keep marketplace publishing human-controlled.
- Add research, visual QA, packaging, and listing automation only after the generation foundation is proven.

## License

This repository currently uses AGPLv3 because the initial ComfyUI client was adapted from the AGPLv3-licensed OpenMontage ComfyUI client. See `docs/open-source-reuse.md` and `LICENSE`.
