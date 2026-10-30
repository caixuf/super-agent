# Illustrated delivery advice

This optional plugin adds a short `pre_llm_call` context note. It helps the
assistant choose when an illustrated document improves comprehension, while
reusing the existing `illustrated-doc-rendering` skill and renderer.

The policy considers the user's requested outcome and the answer's structure:
responsibilities and relationships, stages and dependencies, tradeoffs, or a
reusable explanation. Explicit text-only preferences take priority. Small factual
answers stay inline. Local follow-ups explain the relevant part without creating
another full document. Merely containing a table or exceeding a character count
is not sufficient.

`agentcore.document_intent.advise_delivery` accepts optional `planned_structure`
counts/lists for roles, relationships, phases, steps, alternatives and criteria.
It returns `artifact`, `inline` or `defer` with an explanation. The live pre-call
hook does not yet have the model's planned structure: it supplies conservative
intent hints and an explicit semantic judgment policy. Unknown requests are not
vetoed. This is an advisor, not a claim to recognize all meanings deterministically.

The context asks for plain language: lead with the answer, explain who does what,
introduce necessary terms, and show implementation names only when they help
verification. A visual must explain, not decorate. It also requires actual
rendering and visual inspection before claiming a deliverable exists.

## Settings

```yaml
plugins:
  entries:
    illustrated-delivery:
      settings:
        enabled: true
        engine_root: /absolute/path/to/agentcore-checkout
        include_delegated: false
```

Append `illustrated-delivery` to the existing enabled-plugin list; do not replace
the list or the profile configuration. If the Python package is installed in the
Hermes runtime, `engine_root` is optional. Otherwise it points to the single
canonical engine source. It is administrator configuration, never task data.
No dependency installation or global Python path mutation is required.

The plugin registers no tool, system-prompt section or outbound action. It does
not call a model API, render files, send messages or start a background job.
Delegated turns are skipped by default so the parent owns final delivery.

## Activation

Install the plugin folder under the selected profile's `plugins/illustrated-delivery`
directory. Use Hermes' public settings writer and `cmd_enable` to preserve
unrelated configuration. The current host supports `activate_plugin_now(...,
in_process=False)`: it requests plugin reload over the gateway control socket.
The hook is live after a successful reload; tools and system-prompt sections
would be deferred, but this plugin registers neither. No service restart is
required. If activation reports no gateway response, report it rather than
assuming the running process has loaded the hook.

Validation covers isolated invocation through the real Hermes PluginContext and
PluginManager, plus request and planned-structure examples. These checks prove
the advice arrives, not that a model will always follow it or that a generated
document looks good. Runtime activation, model decisions and visual examples
need their own evidence.

## Stable host API and locations

`advise_delivery`, `select_delivery`, and `choose_delivery` are the same public
advisor. Signature: `(user_message, *, planned_structure=None,
recent_messages=None)`. Result keys include `mode`, `kind`, `reason_code`,
`requires_model_judgment`, `avoid_full_regeneration`, and `suggested_visual`.

When `engine_root` contains the renderer and skill, the hook adds their absolute
canonical locations to its temporary context. A business working directory
therefore cannot accidentally resolve another `tools/arch_doc.py`. Those hints
are paths, not shell commands; the assistant is told not to expose internal
installation paths in the delivered artifact.

Minimal administration script, after installing the plugin directory:

```bash
HERMES_HOME=/absolute/profile HERMES_SOURCE=/absolute/hermes-source \
ENGINE_ROOT=/absolute/agentcore-checkout /absolute/hermes-source/.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, os.environ['HERMES_SOURCE'])
from hermes_cli.plugins_state import save_plugin_setting
from hermes_cli.plugins_cmd import cmd_enable
from hermes_cli.plugins_activation import activate_plugin_now
save_plugin_setting('illustrated-delivery', ('engine_root',), os.environ['ENGINE_ROOT'])
save_plugin_setting('illustrated-delivery', ('enabled',), True)
cmd_enable('illustrated-delivery')
result = activate_plugin_now('illustrated-delivery', in_process=False)
print({'gateway_reloaded': result['gateway_reloaded'],
       'activation': result.get('activation'),
       'restart_required': result['restart_required']})
PY
```

The explicit final activation covers the case where the plugin was already
enabled before its settings changed. Check the response; a disk configuration
change alone does not establish a live hook. This script is provided for the
maintainer and was not run against a production profile by this subtask.
