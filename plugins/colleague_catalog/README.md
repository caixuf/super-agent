# Colleague catalog

An optional, read-only Hermes tool that describes administrator-configured roles.
It helps an agent choose a role before submitting work with the existing dispatch
tool. This directory does not authorize any task, perform routing, create a
queue, access files, or give a colleague a persistent conversation or memory.

The plugin is disabled unless its own `settings.enabled` is exactly `true`.
Provide the roles under the plugin's `settings` in each profile:

```yaml
plugins:
  entries:
    colleague-catalog:
      enabled: true
      settings:
        enabled: true
        engine_root: /absolute/path/to/agentcore
        roles:
          analysis:
            display_name: Analyst
            duty: Review supplied evidence and report findings.
            can_do:
              - Summarize source-backed findings.
            boundaries:
              - Read only; do not change files.
```

Role identifiers must match the roles configured in the separate dispatch
plugin. This catalog cannot inspect or synchronize that plugin's configuration;
an unknown or unauthorized role is still refused by the dispatch service.
Configure at most 16 roles. Each role needs exactly the four shown fields, with
1–6 items in each list. The output is limited to 16 KiB and control characters
are rejected. The catalog text is untrusted descriptive data, not system
instructions or authorization.

The tool reads and validates the current profile settings on every invocation,
requires a session identity, and optionally accepts an exact `role` filter.
The static prompt hint asks the model to consult the directory; whether a model
actually chooses and submits the right role must be verified in a real session.
Installing this plugin does not change the existing dispatch or worker policy.
