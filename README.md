# hermes-delegate-supervisor

[English](README.md) | [日本語](README.ja.md)

A plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that lets the original parent agent periodically review child agents running through `delegate_task`. The plugin ID is `delegate_supervisor`, and the default review interval is 600 seconds (10 minutes).

A timer tracks review deadlines and passes the targets to the parent. The parent can inspect progress and evidence as needed, then send further instructions through the existing `delegate_task(action="steer", ...)`. This plugin does not launch a separate supervision model. The timer and liveness checks do not call a model, but the parent's review turns incur normal inference costs.

This README describes the **v0.3.0 source**, accepted after independent review and offline verification. v0.1.0 does not have `/supervision`; v0.2.0 does not have `enabled_by_default` or `/supervision on`. Delivery and parent decision-making have not been verified with real models or live Gateway, Discord, CLI, TUI, or Desktop operation.

## What it supervises—and what it does not

The plugin supervises child agents launched with `delegate_task` from top-level conversations after the plugin has loaded. When several review deadlines for the same parent coincide, it combines them into one review. It does not retroactively register children that were already running before it loaded.

The following remain the responsibility of Hermes:

- Starting, running, and stopping children; completion notifications; and stall detection.
- Selecting child models, providers, inference settings, and Fast settings.
- The parent's decisions and further instructions to running children.

This is not a monitor for general background processes, cron jobs, or arbitrary task lists. Parents of nested delegations, API servers, stateless or one-shot sessions, and conversations without an identifiable delivery destination are out of scope. The plugin does not register or modify the existing `/heartbeat`. Heartbeat provides arbitrary recurring instructions per conversation; this plugin instead tracks review deadlines for running delegated children.

## `/supervision` within a conversation

The command displays or changes settings for the current conversation without calling a model. It does not write to other conversations, other profiles, or configuration files.

| Input | Effect |
|---|---|
| `/supervision` or `/supervision status` | Shows enabled/disabled state, effective interval, each setting's default/override source, target IDs, parent busy/idle state, pending count, and whether a review request has been accepted |
| `/supervision on` | Enables automatic reviews for this conversation; preserves any interval override, otherwise uses the default interval read at load time |
| `/supervision 10m` | Overrides the interval for this conversation and enables automatic reviews |
| `/supervision off` | Disables automatic reviews only; retains the previous interval and registered children |
| `/supervision default` | Clears both enabled-state and interval overrides and restores the settings read at load time; if the default is off, reviews become disabled |

Intervals are integers or decimals followed by `s`, `m`, or `h`. Examples include `30s`, `1.5m`, and `1h`; the minimum is 1 second. Values without a unit and `1d` are not accepted. Shorter intervals increase the number of parent review turns and their inference costs.

You can configure supervision before launching a child. If the conversation does not yet have a session in the Gateway, however, send a normal message first. The plugin does not create a new session just to handle the command.

### Changing the interval versus disabling reviews

Changing the interval or using `on` or `default` preserves registered children and the parent's state. Changes that enable reviews also preserve pending reviews and accepted requests. Each child's next deadline is recalculated from the time the command is accepted, using the effective interval. Reviews that are already pending or accepted may arrive before that new deadline. No plugin reload is needed for these changes.

`off` does not stop children. It preserves the interval and registered children, clears pending reviews, and prevents review targets from being supplied at the start of a turn. A default-off conversation, including after `default` restores that policy, also supplies no review targets. Children launched while supervision is disabled are still registered, so `on` can begin supervising children already running in that conversation. Re-enabling supervision sets the next deadlines from the current time.

In Classic CLI, unconsumed review requests are invalidated. Gateway, TUI, and Desktop have no API for retracting accepted input, so one accepted request may still arrive after supervision is disabled. That request alone does not prompt further instructions to children. However, a parent turn that has already started with review targets is not canceled.

### Supported interfaces and behavior while busy

| Interface or route | Handling of `/supervision` |
|---|---|
| Classic CLI | Uses native command handling |
| TUI and Desktop | Uses native plugin invocation; control calls while busy have been verified offline |
| Idle Messaging Gateway | Handles it as a registered command |
| Busy Messaging Gateway | Explicitly rejects it on supported hosts and adapters; use the command after the parent becomes idle |

A busy Gateway rejects the command after user authorization, bot admission, and slash-command permission checks. It does not forward the command to the normal input queue, model steering, or interrupt handling, and does not change the settings.

This rejection depends on a narrowly scoped adapter for host internals. On an unknown implementation, rejection is disabled with a warning, and native handling may treat the command text as normal input. Safe control while busy is not guaranteed without confirming compatibility.

## Installation

The Python package declares Python 3.10 or later as its requirement. Actual compatibility depends on Hermes APIs and internal implementation. The verified host is `v0.21.5+4775.g3ebbaf5`, at source revision `3ebbaf524344f93943169e63854cb952541563f9`. Compatibility with other revisions, including the latest version, has not been verified. Read [Compatibility and limitations (Japanese)](docs/compatibility.md) first.

The repository supports both native directory plugins and Python entry points. In a managed runtime, use Hermes's official plugin management commands. The following is an installation example for use **after the repository has been published**. Set `PUBLIC_COMMIT_SHA` to the 40-character SHA of a reviewed public commit.

```sh
PUBLIC_COMMIT_SHA=REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT_SHA
hermes plugins install teich0psia/hermes-delegate-supervisor \
  --ref "$PUBLIC_COMMIT_SHA" --no-enable
hermes plugins show delegate_supervisor
```

Select the target profile explicitly through the normal Hermes profile selection mechanism or `HERMES_HOME`. `--no-enable` installs the plugin without enabling it. Replacing an already enabled version requires a separate check of state and permissions; this example covers a new installation only.

Sending automatic reviews to TUI, Desktop, or Gateway requires permission equivalent to `gateway.inject`. Configure the default interval read at load time and injection permission with these keys:

```sh
hermes config set plugins.entries.delegate_supervisor.settings.interval_seconds 600
hermes config set plugins.entries.delegate_supervisor.allow_gateway_injection true
hermes plugins enable delegate_supervisor --no-allow-tool-override
```

Permission to override built-in tools is not required. The narrowly scoped Classic CLI FIFO route does not use Gateway injection permission. The configured interval must be a finite positive number; strings, booleans, 0, negative numbers, NaN, and infinity cause the plugin to become inactive.

To use automatic reviews only in conversations that opt in, keep the native plugin enabled and configure:

```sh
hermes config set plugins.entries.delegate_supervisor.settings.enabled_by_default false
```

`enabled_by_default` controls the default automatic-review policy for new conversations, **not native plugin enablement**. When omitted, it is `true` for backward compatibility: v0.3.0 is not inherently default-off. With `false`, child registration and `/supervision` remain available, but no automatic wake or supervision-target context is generated until that conversation uses `on` or a duration. Only booleans are accepted; strings such as `"false"`, numbers, and null cause the plugin to become inactive with a warning. Default-off prevents automatic review inference, but does not stop the plugin's timer or liveness checks.

Depending on the host, `enable` requests a plugin reload in the running Gateway. This is distinct from a service restart, but the plugin's supervision registrations and conversation overrides are lost. If conversations are active, check the effects of a reload before enabling the plugin. Both default enabled state and default interval in the configuration file are read at load time; use `/supervision` within a running conversation for immediate changes. After loading with `enabled_by_default: false`, use `/supervision on` in a new conversation to opt in.

## Review request delivery and conversation lifecycle

- Each child's deadline is tracked with a monotonic clock. If the parent is busy, reviews remain pending: the plugin supplies the targets at the next naturally occurring turn boundary, or reserves a review turn once the parent becomes idle. It does not interrupt an active parent turn for supervision.
- Each reservation has a unique token. Further reservations are suppressed until that request reaches the turn boundary. Successful acceptance is not proof that a model ran or a reply was delivered. Unrelated user turns are not treated as delivery acknowledgments.
- At consumption time, Classic CLI rechecks the conversation generation and reservation validity, discarding only this plugin's invalidated requests. Normal input retains FIFO ordering. Gateway checks the generation after asynchronous session resolution and again at the execution boundary.
- Supervision registrations are removed when a child completes or stops, when the parent stops, resets, or finalizes, and when the plugin unloads. A parent stop preserves conversation settings; reset and finalize clear them as well. Conversation overrides remain even after the last child finishes.
- Settings and supervision carry over only when committed compression history establishes an unambiguous continuation. They do not carry over to a resume or reset into another session, or when history is reused for a different delivery destination. Migration is also refused when history is ambiguous, the database cannot be read, or the implementation is unknown.
- State is held only in memory within the process. Supervision registrations and conversation overrides are not restored after unload, reload, or restart. This state is independent of Hermes's persisted completion delivery.

## Development and verification

The accepted v0.3.0 offline verification recorded **130 passing tests for the source** and **130 for the unpacked wheel**; independent review found no material issue. This covers default-policy validation, `on/off/default`, preserved child registrations while disabled, conversation lifecycle and isolation, and the existing delivery and busy-path regressions. The older v0.2.0 acceptance recorded 99 passing tests each and is separate evidence. These checks connect actual host logic to fixtures; they do not establish successful child launches with a real LLM, authentication, Discord delivery, or live startup of each interface.

Public-release preparation reuses that accepted functional evidence and keeps runtime modules byte-identical to the accepted implementation. Its checks are limited to export consistency, distribution packaging, documentation links, whitespace, privacy, and the host's offline Doctor API; the full functional suite is not rerun. Raw operational and independent-review records remain outside the public tree.

See [Development instructions (Japanese)](docs/development.md) for verification procedures that do not alter the environment and for the required external checkouts. An `ACTIVE` log at registration time or a successful Doctor check alone does not establish that a long-running Gateway has adopted that version, or that periodic reviews are actually being delivered.

## License and provenance

This project uses the [MIT License](LICENSE). Use, modification, redistribution, and commercial use are permitted. When redistributing copies or substantial portions, retain the copyright notice and permission notice. The software is provided without warranty.

Hermes Agent itself and third-party dependencies remain subject to their respective licenses. Tests read host logic from a separate checkout and run it with fixtures. This project does not bundle Hermes itself or a separate routing plugin.

## References

- [Hermes plugin development guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/)
- [Delegation and steering running children](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation/)
- [Session Heartbeat](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/)
