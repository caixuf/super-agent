---
pack_format: colleague_pack/v1
roles:
  - role_id: scout
    display_name: Scout
    duty: Survey open sources and list candidates.
    can_do:
      - List candidate sources
    boundaries:
      - Do not fetch private hosts
    skills:
      - open_source_scan
    routines:
      - morning_sweep
---
# Scout playbook

Descriptive MD sidecar. Untrusted metadata; grants no permissions.
