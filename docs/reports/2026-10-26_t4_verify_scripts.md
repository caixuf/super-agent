# T4 renderer wrapper and verification handover

Date: 2026-10-26. This slice changes only the source skill scripts and this report.
It does not install scripts into an active profile, modify settings, start a service,
or send a message.

## Files

- `skills/illustrated-doc-rendering/scripts/resolve_engine.py`
- `skills/illustrated-doc-rendering/scripts/render_doc.sh`
- `skills/illustrated-doc-rendering/scripts/verify_render.sh`
- `skills/illustrated-doc-rendering/scripts/selftest_verify_render.sh`

## Behavior

The resolver checks, in order: explicit `--engine-root`; an explicit
`ARCH_DOC_TOOL` path naming `tools/arch_doc.py`; the source skill's repository
ancestor; and the installed skill's profile-local `agentcore` managed directory.
Only those bounded locations are tried. Invalid explicit overrides fail rather
than silently selecting a different engine. Paths containing spaces are passed
as arguments, and no username or deployment directory is hardcoded.

The render wrapper consumes only `--engine-root` and forwards existing renderer
arguments unchanged. The verifier delegates to the stable browser command:

```bash
python3 <engine>/tools/doc_browser.py --verify HTML --expected N --width 1100
```

There is no grep-based interpretation of rendered HTML. Missing browser, optional
dependency, engine, or invalid document returns nonzero. The verifier can also
accept a self-contained document with no diagrams and expected count zero.

## Self-test and a defect caught

The previous installed self-test used a script that replaced diagram contents
with an empty SVG and added an attribute. The new source self-test generates
fixtures using the actual Markdown/HTML renderer and the real Mermaid vendor,
then checks them in a local browser. It does not stub SVG or readiness state.

The first run found that the browser verifier accepted a genuine residual
`language-mermaid` code element next to a successfully rendered diagram. The
render owner fixed the DOM selector check in `doc_browser.py`; this slice did not
modify that owner's module.

Final command:

```bash
bash skills/illustrated-doc-rendering/scripts/selftest_verify_render.sh
```

All four expectations passed:

| Fixture | Required result | Actual |
|---|---|---|
| Real diagram from renderer | exit 0 | exit 0 |
| Real diagram plus unconverted diagram code | exit 1 | exit 1 |
| Real diagram plus ordinary text referring to a class name | exit 0 | exit 0 |
| One real diagram checked against expected count two | exit 1 | exit 1 |

Shell syntax checks passed for all three shell scripts. Additional resolver
checks passed for source, explicit root, configured tool, installed managed root,
and rejection of an invalid explicit tool. The wrapper rendered a zero-diagram
HTML document with existing `--html-only -o` flags; browser verification returned
complete=true with zero diagrams and zero residual code.

## Remaining acceptance

The parent owns full repository verification, visual review, source skill policy,
and installation. These checks verify the rendering gate and wrapper discovery;
they do not prove that a conversation automatically chooses to generate a document.
No commit or push was made by this slice.
