# Prompting GPT Image (Codex `image_gen`)

Use with `porch media image generate|edit|animate -a codex…`. Codex CLI's
built-in `image_gen` tool calls `gpt-image-2` (codex-cli 0.167.0 source) and
returns PNG. The guidance below is paraphrased from OpenAI's official guides;
read them for illustrated examples.

Sources (newest first):

- OpenAI API docs, [Image prompting](https://developers.openai.com/api/docs/guides/image-prompting)
  (GPT Image 2.6 prompting guide; the fundamentals apply to `gpt-image-2`)
- OpenAI Cookbook, [GPT Image Generation Models Prompting Guide](https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide)
  (`gpt-image-2`, April 2026; now marked archived)
- OpenAI API docs, [Image generation](https://developers.openai.com/api/docs/guides/image-generation)

Codex's own bundled skill (`~/.codex/skills/.system/imagegen/SKILL.md`)
governs how the tool saves files; porch collects them from the session store.

## Fundamentals

1. **Define the result.** Name the subject and the intended use (product
   photo, ad, infographic, UI mock-up), the composition, aspect ratio and key
   placements. For complex requests use labeled sections in the order scene →
   subject → details → constraints.
2. **Pick a maintainable format.** Paragraphs, short instructions, tags or
   JSON-like blocks all work; clarity beats clever syntax.
3. **Describe visible details.** Materials, lighting, colors, medium. Say
   "photorealistic" or "real photograph" to engage the photoreal mode; treat
   camera specs as cues for the look, not exact physics. For moody wide,
   night, rain or neon scenes, spell out scale, atmosphere and color.
4. **People and action.** Body framing ("full body, feet included"), relative
   scale, gaze ("looking down at the open book"), how hands hold objects.
5. **Exact text.** Quote the copy, say how many times it appears, where, and
   in which typography; spell unusual names letter by letter; ask for "no
   extra text"; check spelling afterwards.
6. **Changes vs constraints.** For edits: "change only X", then list what to
   preserve (identity, geometry, layout, lighting, labels, camera angle) and
   repeat that list on every iteration. Exclude watermarks, logos, stray text
   explicitly.
7. **Roles for references.** Number each input and give it a purpose
   ("Image 1: product photo; Image 2: style reference — apply Image 2's style
   to Image 1"), and say which element moves where.
8. **Iterate one change at a time,** feeding the previous output back as the
   input and restating critical constraints if the result drifts.

## Use-case patterns from the guide

- **Infographics, diagrams, slides:** write it like an artifact spec — exact
  deliverable, audience, canvas, hierarchy, and the real labels and numbers.
  Verify facts and any numbers the model adds.
- **Logos:** describe the brand and the defining shapes, ask for a strong
  silhouette that reads small and large, flat vector-like style, generous
  padding; request an original, non-infringing mark.
- **UI mock-ups:** describe the product as if it already shipped (layout,
  hierarchy, spacing, real components), not as concept art.
- **Comics and storyboards:** one concrete visual beat per panel.
- **Historical scenes:** name the place and date; check period accuracy.
- **Looping GIFs:** give the subject a starting pose to return to, say
  whether the camera moves, and ask for a smooth loop.

## Parameters and limits (`gpt-image-2`)

- Sizes: edges multiples of 16, long:short ratio at most 3:1, at most
  8,294,400 total pixels and at least 655,360, longest edge under 3840 px;
  above 2560×1440 results are less predictable. Common sizes: 1024×1024,
  1536×1024, 1024×1536. In Codex the model chooses the size from your words —
  state the ratio (`--aspect` adds it to the prompt).
- Quality `low|medium|high`: use medium/high for small text, dense diagrams
  and identity-sensitive edits. `input_fidelity` does not apply; inputs are
  always high fidelity.
- Transparent background (preview): PNG or WebP only, and the prompt should
  describe an isolated subject on a fully transparent background with no
  scenery, checkerboard or shadow; repeat "keep the background transparent"
  on edits.
- Codex edits use images in the conversation context — porch attaches every
  `--input` with `--image` — and accept up to 5 references.
- Native animated GIF output (2–16 frames) exists in the Images API and in
  Codex's source behind the `image_generation_animation` feature, which is not
  in codex-cli 0.167.0; `porch media image animate` therefore renders a
  spritesheet and assembles the GIF itself.

## Example prompts (written for porch)

- Generate: `Photorealistic candid photo of a cyclist fixing a chain on a rainy Amsterdam bridge at dusk, medium shot at eye level, hands on the derailleur, warm streetlights reflecting on wet cobblestones, subtle film grain. No text, no logos. 3:2 landscape.`
- Infographic: `Create a clean classroom slide titled "How a Heat Pump Works": four labeled stages (evaporator, compressor, condenser, expansion valve) connected by arrows, flat icons, white background, readable sans-serif labels, no decorative clutter. 16:9.`
- Logo: `Create an original logo for "Juniper & Ink", an independent bookshop: a simple juniper sprig forming a quill, flat two-color vector style, strong silhouette, generous padding, transparent background.`
- Edit (`--input room.png`): `Change only the sofa to deep green velvet. Keep the room layout, lighting, camera angle, wall art and every other object exactly the same.`
- Combine (`--input product.png --input style.png`): `Image 1 is the product, image 2 is the style reference. Render the product from image 1 in the watercolor style of image 2 on a plain off-white background, keeping its shape and label legible.`
