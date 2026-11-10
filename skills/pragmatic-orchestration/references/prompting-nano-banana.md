# Prompting Nano Banana (Gemini image models via `agy`)

Use with `porch media image generate|edit|animate -a agy-…`. Antigravity's
`generate_image` tool runs a Gemini image model — observed on 2026-11-10 as
Gemini 3.1 Flash Image, "Nano Banana 2"; the CLI offers no model choice. The
guidance below is paraphrased from Google's official guides; read them for the
illustrated examples.

Sources (newest first):

- Google Cloud, [Ultimate prompting guide for Nano Banana](https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-nano-banana)
  (2026-03-05; Nano Banana 2 and Nano Banana Pro)
- Google DeepMind, [How to create effective image prompts with Nano Banana](https://deepmind.google/models/gemini-image/prompt-guide/)
- Google, [Nano Banana Pro prompt tips](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/) (2025-11-20)
- Gemini API, [Nano Banana image generation](https://ai.google.dev/gemini-api/docs/image-generation) (best-practices section)
- Google Developers Blog, [How to prompt Gemini 2.5 Flash Image](https://developers.googleblog.com/how-to-prompt-gemini-2-5-flash-image-generation-for-the-best-results/)
  (2025-08-28; older model, the principles still apply)

## Core principle

Describe the scene as a narrative paragraph instead of a keyword list — the
model reasons about the whole prompt before drawing. Open with a strong verb
that names the operation (create, edit, combine, restyle), then direct it like
a creative director.

## Structure

- **Text only:** subject → action → location/context → composition → style.
  Be concrete: "ornate elven plate armor etched with silver leaf" beats
  "fantasy armor".
- **With references:** references → how they relate → the new scenario, for
  example "use image 1 as the structure and image 2 as the texture, render it
  as a 3D armchair in a sunlit minimalist room". Give each input a role (pose,
  style, background) and name recurring characters or objects so they stay
  consistent.
- **State the purpose** ("a logo for a minimalist skincare brand", "a
  vertical social post") — intent changes polish and layout.
- **Complex scenes:** give steps — background first, then foreground objects,
  then the focal detail.

## Direction vocabulary

- Camera and lens: wide-angle, macro, low angle, aerial view, 85 mm portrait
  lens, shallow depth of field (f/1.8), Dutch angle; a named camera type
  (GoPro, disposable flash camera) shifts the whole look.
- Lighting: three-point softbox, chiaroscuro, golden-hour backlight.
- Color and film stock: "1980s color film, slightly grainy", "cinematic grade
  with muted teal tones".
- Materials: name the fabric, surface or finish (navy tweed, matte ceramic).

## Editing

- Say exactly what changes and what must stay the same; the model can edit a
  region described in words (semantic masking) while leaving the rest intact.
- Add objects by supplying the object image and saying where it goes; restyle
  by asking for the same content in another style.
- Edits normally keep the input's aspect ratio; say "do not change the aspect
  ratio" if it drifts. With several inputs of different ratios, the last one
  tends to win.
- If a character drifts after many edits, restart from a fresh, detailed
  description.

## Text in images

Put the exact words in quotes and describe the typography (font family or
style, weight, color, placement). For other languages, write the prompt in one
language and name the target language; supply the exact translated text in
quotes when wording matters. Small text and exact spelling are still a known
weak spot — check the output.

## Negative instructions

Phrase exclusions positively: "an empty, deserted street" rather than "no
cars".

## Format and limits

- Aspect ratios: 1:1, 3:2, 2:3, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9 (Nano
  Banana 2 adds 1:4, 4:1, 1:8, 8:1). Ask for the ratio in words as well
  (`porch media … --aspect 16:9` does this) and name the format ("widescreen
  backdrop", "vertical poster").
- The API accepts up to 14 reference images and renders 0.5K–4K; agy's
  `generate_image` takes at most 3 references (porch enforces this) and saved
  1024–1376 px JPEGs in testing.
- Known limitations (Google): small text and fine detail, factual accuracy
  of diagrams, multilingual grammar, unnatural artifacts in complex blends,
  and variable character consistency. Every image carries a SynthID watermark.

## Example prompts (written for porch)

- Generate: `Create a photorealistic product shot of a matte black ceramic pour-over coffee set on a walnut counter, morning window light from the left, shallow depth of field, steam rising from the cup, clean negative space on the right for copy. 16:9 widescreen.`
- Text: `Create a vintage travel poster for "LISBON" in bold cream art-deco letters across the top, a yellow tram climbing a steep tiled street at sunset below, flat screen-print style with a limited warm palette. 2:3 vertical.`
- Edit (`--input photo.jpg`): `Change only the sky to a dramatic stormy dusk with low purple clouds; keep the buildings, people, framing and colors of everything below the roofline exactly the same.`
- Combine (`--input character.png --input scene.png`): `Place the fox character from image 1 into the forest clearing from image 2, sitting on the mossy log in the foreground, matching the scene's soft morning light and painterly style.`
- Animate (`porch media image animate`): `A paper boat bobbing on gentle ripples, camera still, starting and ending in the same position so the loop is seamless.`
