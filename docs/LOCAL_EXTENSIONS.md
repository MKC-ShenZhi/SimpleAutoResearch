# Trusted local extensions (v1)

Add `extensions = ["../my-skill"]` to the existing `[task]` table in a task
TOML, then use `simple-ar start --task-config path/to/task.toml`. Entries select
an explicit directory containing `extension.toml` or a descriptor file. Paths
are resolved relative to that task configuration and saved as absolute paths.
There is no directory scanning, download, install, environment-variable entry,
global allowlist or separate extension task state.

Copy `examples/local_extension` anywhere on the execution machine to try it.
Mount a local notes directory with the ordinary task `source` setting. The
model sees the short `local_notes__instructions` and `local_notes__count_words`
catalog entries; the normal `load_tools` operation selects their schemas.
Calling the instructions tool loads the skill into ordinary tool history.
Calling `count_words` invokes the declared Python function through the
same registry, common gateway, permission policy, trace and native attempt
controller as built-ins. The skill may also combine existing host tools.
Full-contract tasks expose these schemas immediately; instruction bodies still
require an explicit tool call. Skill-only and tool-only descriptors also work.

The descriptor declares a stable ID, version, description, scope, optional
instruction file and explicit resources, plus optional tool contracts. IDs and
local names use lowercase letters, digits and underscores; public names are
`ID__name`. Duplicate namespaces, names and conflicting domain owners fail.
`instructions` is reserved as a local tool name. Resources must resolve inside
the selected package. Tools declare a single Python file and function, called
as `function(arguments, services) -> dict` with a JSON object result. The
implementation must be a `.py` source file.

Each implementation file is loaded lazily as a standalone Python module, once
per extension owner in the current process. Tools using the same resolved file
share that module. It has `__file__`, `__spec__`, an empty `__package__`, and a
unique internal `__name__` registered in `sys.modules` before execution, so
dataclasses, postponed annotations and ordinary module lookup work. Separate
owners have separate module identities. Failed imports remove their registration.
Successful modules remain registered for the process lifetime. The loader
compiles the verified source bytes directly, without reading or writing `.pyc`
caches or adding the package directory to `sys.path`.

Absolute imports use the installed host environment. Package-relative imports
and automatic sibling-module imports are unsupported; a resource declaration
does not make a companion Python file importable. Keep extension logic in the
declared implementation file. Module globals are transient and reset when the
task is loaded into a new owner, including resume. Do not use globals as durable
task state or rely on import-time side effects for recovery.

Input schemas use the standard Draft 2020-12 validator. Schemas and arguments
are checked before invocation. In-document `$defs`/`$ref` work; unresolved
references fail as tool errors. The reference registry explicitly disables
retrieval, including HTTP and `file:` resources; declared companion files are
not a schema registry. No custom keyword subset is substituted for JSON Schema.
As in the standard validator, `format` is annotation-only without a format
checker. Declare object properties and required fields explicitly.
The service map supplies only `call_tool(name, arguments)`, restricted to host
workspace read/plan operations and still checked by the host policy. Material
locators cannot escape current mounts. Nested operations derive distinct,
recoverable identities from the parent attempt and retain native receipts.
No model client, ledger mutation or process service is
passed to Python. Use existing host tools for writes, processes and network.

V1 accepts Python tools declaring `read_only`/`plan` only; authors must ensure
their functions are deterministic and free of side effects. Other permission
declarations fail explicitly; installing a package never grants those rights.
Python is trusted and has the host process's OS privileges: these service
restrictions are **not a sandbox** and cannot constrain malicious Python.
Do not load untrusted packages. Direct Python file, process and network access
remains possible with the host's privileges, regardless of declarations or
host-service policy. Imported dependencies use the installed host environment;
v1 does not freeze or copy that environment. List local companion resources
explicitly to include them in package identity checks; undeclared resources
and dependencies are outside those checks.

This delivers the usable local skill/custom-operation slice, not closure of all
P2 or all §9.6 acceptance requirements. The remaining permission consumers are:

- Execution: `CommonToolGateway.session_owner` admits context-bound owners via
  `context_tools`; `ToolApplication._owner_execution_binding` then requires
  `approval_binding`, and `_tool` requires `execute_tool`, `has_started` and
  saved approval. `approval_token` otherwise falls back to built-in process or
  publication inputs. Arbitrary extension arguments cannot use that fallback.
- Network: `_configure_tools` can enable the network category for vision/image
  access while separately blocking source tools. That is not a destination or
  purpose grant for an extension. No generic extension network-service binding
  exists, so even `allow_network=true` does not admit an extension declaring
  `network` permission. Trusted Python can still make direct network calls.
- Writes and incomplete side effects: `CommonToolGateway.can_reconcile` and
  `ToolApplication._attempt` require owner-backed durable recovery evidence.
  Workspace writes already have their own bindings/receipts; extension v1 does
  not adapt arbitrary Python mutations into those transactions. `write_patch`,
  `execution`, `network` and `secret` descriptors are rejected before import.

The read/plan path uses the native attempt budget and ordinary retained result
recovery. No private approval, scheduler, billing or recovery runtime is added.

Saved task configuration retains descriptor, code and explicit resource hashes,
version and at most 16 KiB of instruction text per package. Startup does not
import Python or put instruction bodies in model context. Resume and execution
reject missing or changed selected packages. Restore the original files to
continue; automatic migration or hot replacement is unsupported. Existing
attempt artifacts remain ordinary readable files even if application loading
rejects extension identity. Identity checks themselves do not delete or rewrite
old artifacts or budgets. This is hash-based identity verification against the
original local paths, not a copied, immutable or hermetic package snapshot.
Verification precedes every call, even for cached modules; executable source
is separately hashed immediately before loading or reusing the module. Keep
packages unchanged while running: companion-resource reads and later imports
are not atomic with verification, and concurrent edits are unsupported.
Closed results resume through native receipts without repeating Python. An
interruption before any durable result uses the existing explicit failure path;
v1 does not promise replay or exactly-once effects. Native attempt budgets apply;
`CommonToolSpec.max_calls` is not a separate enforced invocation budget.

New tasks give each nested host operation its own recoverable identity: two
searches inside one extension call must not reuse each other's results. Saved
tasks retain their original identity protocol; a changed retained command is
refused, not replayed. Extension argument names do not implicitly enable
built-in writing, publication or execution behavior.

Server-side offline verification, from the repository root with this development
package installed:

```sh
python -m unittest tests.test_local_extensions.LocalExtensionContractTests
python -m unittest tests.test_local_extensions.LocalExtensionIntegrationTests.test_local_package_consumer_recovery_and_identity
```

This uses a mocked model and a package copied outside the task/project. It
checks the actual model catalog/schema consumer, skill loading, mounted read,
permission rejection, trace/attempt records, interrupted-result recovery,
collision rejection and missing/changed identity with readable saved artifacts.
The bounded contract suite also checks registered dataclass/annotation support,
module reuse and owner isolation, failed-import cleanup, unsupported relative
imports, standard local schema references without retrieval, companion-resource
identity and instructions-only packages. Both commands use no paid API. These
checks do not replace ordinary-user acceptance of the complete persistent-project
work package.
