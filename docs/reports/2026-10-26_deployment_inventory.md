# Deployment inventory and T6 preflight gaps

Date: 2026-10-26. This is a read-only inventory. No installer, profile, service,
credential, upstream checkout, or container was changed. Credential values were
not printed; the checks below report only names, injection source, and a boolean
for whether a value or credential fingerprint is present.

## Installer coverage

`install/install.sh` currently copies these repository-owned assets:

| Asset | Actual installer behavior | Gap against the active profile |
|---|---|---|
| Identity | Installs the generic identity and agent contracts. Writes the active identity only when an explicit composition/deploy option is used. | Profile identity overlays and profile-specific agent instructions are not in the generic repository. No owner manifest maps them to the deployment overlay. |
| Toolsets | Copies every directory under `toolsets/`. | Does not install host-side tools or register profile configuration that enables them. |
| Runtime | Copies files under `runtime/`; today this is a service skeleton and documentation. | Does not create the actual service, copy the live profile configuration, or pin its environment. |
| Patches | Copies all files under `patches/`. It automatically checks/applies only the existing chat-card adapter patch. | The terminal-card patch and dispatch visibility patch are copied as files but are not part of the installer apply/check sequence. The extra preview cleanup patch is copied but has no installer action. |
| Skills | Installs repository `SKILL.md` files into the selected profile and skips `skills/examples/`. | Installs the one production skill currently in this repository; it does not install the profile’s other 65 skills or identify their source owner. |
| Plugins | No `plugins/` install or plugin-setting reconciliation exists. | None of the enabled plugins is installed by this installer. This is the largest direct deployment gap. |
| Dependencies | No Hermes installation, Python environment, package installation, Node runtime, plugin dependencies, or credentials are provisioned. | Installation can copy assets without proving that any plugin or skill can import and run. |

The repository has six plugin directories at this snapshot: `capability-alert-outbox`,
`durable-dispatch`, `illustrated-delivery`, `localvec`, `longtask-guard`, and
`write-guard`. The active profile configuration enables seven plugins:

| Enabled plugin | Repository source | Live source observation | Installer result |
|---|---|---|---|
| `advanced-delegation` | Absent | Present in profile plugin directory | Not installed |
| `durable-dispatch` | Present | Present; Python source differs from repository source | Not installed |
| `illustrated-delivery` | Present | Present; source matches, only bytecode differs | Not installed |
| `langfuse` | Absent | Present in profile plugin directory | Not installed |
| `longtask-guard` | Present | Profile path is a symlink to repository source | Not installed |
| `mcp-serve` | Absent | Present in profile plugin directory | Not installed |
| `write-guard` | Present | Profile path is a symlink to repository source | Not installed |

`localvec` is present in both the repository and profile plugin directory but is
not in the enabled list. `capability-alert-outbox` is a new repository directory
and is not installed or enabled. No plugin dependency lockfile or per-plugin
installation manifest was found. The root `pyproject.toml` declares no required
runtime dependencies; its test and document extras are optional and are not
installed by `install.sh`.

The profile has 66 skills. One matches the repository-managed production skill;
58 also exist in both the global skills tree and the upstream Hermes skills tree;
seven are profile-only relative to those sources. Six profile-only names are
`devops/gh-repo-publish`, `devops/hermes-pretool-hook-guard`,
`devops/llm-model-identity-routing`, `devops/remote-bench-execution`,
`subsystem-extraction-refactor`, and `terse-instruction-intent-recovery`.
The remaining name is profile-specific and intentionally omitted from this
general repository report. None of these 65 non-repository skills is copied by
this repository’s installer.

### Patch checks and drift

The two read-only patch checks succeeded:

| Command | Result |
|---|---|
| `python3 patches/apply_feishu_card_replies.py --check` | PASS: all three content probes are present. Reverse `git apply` was false; the script documents this as informational because comments can drift. |
| `python3 patches/patch_card_finalize.py --check` | PASS: all eleven content probes are present. |

The repository also contains `patch_durable_dispatch_visibility.py` and its
verification script, but neither is invoked by `install.sh`. The installer has
no declared Hermes upstream revision or supported version range.

## Hermes and patch compatibility evidence

The active Hermes source checkout reports `v0.22.5-dirty`, at commit
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`. Its worktree is dirty: five tracked
gateway/platform/tool files are modified and one local package is untracked.
This does not establish that the checkout matches an upstream release or a clean
clone. The shared repository is at `f5ec917f2c6c16e8eac2a81c1d109877d8e00573`;
these are separate repositories and revisions.

The patch scripts already expose a useful read-only interface:

- `--check`: zero means the expected probes are present; a nonzero result means
  absent or unsupported, depending on the script.
- `--apply`: idempotent application with explicit anchor failure.
- The first card patch also has `--config`, which needs PyYAML and a profile path.

The current installer invokes only one `--check` and may immediately apply that
patch after a failed check. For a new upstream revision, a failed check should
stop before mutation and require review in a scratch checkout. The version
string and patch probes are not yet bound together in a compatibility manifest.

## Credential onboarding inventory

The following entries were present in the global/profile environment files or
the profile’s Hermes credential-pool metadata. Values were reduced to booleans
before output. “Configured” means nonempty text or a stored credential
fingerprint was present; it does **not** prove authentication succeeds.

| Credential name / provider alias | Injection source | Configured |
|---|---|---:|
| `OPENDESIGN_API_KEY` | Global and profile dotenv files | true |
| `TAVILY_API_KEY` | Global and profile dotenv files | true |
| `AUTOTEST_LLM_API_KEY` | Profile dotenv file | true |
| `COMMANDCODE_API_KEY` | Profile dotenv file; provider pool aliases also refer to this environment key | true |
| `FEISHU_APP_SECRET` | Profile dotenv file | true |
| `OPENAI_API_KEY` | Profile dotenv file; provider pool metadata refers to the environment key | true |
| `HERMES_LANGFUSE_SECRET_KEY` | Profile dotenv file | true |
| `LANGFUSE_SECRET_KEY` | Profile dotenv file | true |
| `AUTOTEST_API_TOKEN` | Profile MCP dotenv file | false (entry is empty) |
| `MEMORY_LLM_API_KEY` | Profile MCP dotenv file | true |
| `MEMORY_EMBEDDER_API_KEY` | Profile MCP dotenv file | true |
| `commandcode`, `commandcode-anthropic`, `commandcode-chat`, `commandcode-claude` | Hermes credential pool; environment-key source | true (fingerprint present) |
| `openai-api` | Hermes credential pool; environment-key source | true (fingerprint present) |
| `custom:ccp-codex`, `custom:litellm` | Hermes credential pool; provider-config source | true (fingerprint present) |

The repo installation path should turn this into an executable, value-safe
preflight rather than a template of secrets:

1. Add a generic `credentials.manifest` with credential name, scope (global,
   profile, or MCP process), required/optional status, injection method, and a
   non-network presence check. Keep values in the operator’s secret store or
   mode-0600 environment files; never copy them into the repository or image.
2. Provide a setup command that asks the operator to populate each required
   name, checks file permissions and nonempty status, then prints only
   `name / source / configured` booleans. Optional missing entries should be
   reported separately from required failures.
3. Keep Hermes OAuth/provider credential setup on Hermes’ supported auth or
   credential-pool flow. Do not infer a usable login from a fingerprint, and do
   not write copied `auth.json` contents into a new profile.
4. Add a separate opt-in connectivity probe only when an operator requests it;
   installation and dry-run should not call a model or send a platform message.

## Suggested upstream/patch drift interface

Add a versioned compatibility manifest to the deployable repository with the
Hermes repository identity, exact tested commit, supported release range, and
for each patch: target files, patch digest, required probe identifiers, and
read-only check command. A read-only `install --preflight` should print the
current Hermes commit/version, each patch check, required runtime/dependency
presence, and credential-name booleans. Unknown upstream commits or missing
anchors should fail closed before applying anything. Applying a patch remains a
separate explicit action; a new upstream revision should be tested in a clean
scratch checkout before its compatibility entry is added.

## Evidence limits

This is source and filesystem inventory, not a clean-room install. No container
was built, no dependency was installed, and no credentials were validated
against an external service. The two patch `--check` commands were read-only and
passed on the current dirty Hermes checkout. The exact upstream comparison,
plugin activation after a clean install, and deployment equivalence remain for
the root-owned final T6 acceptance.
