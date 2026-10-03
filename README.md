# T3 Code Monitor for Home Assistant

Monitor an **already-running** T3 Code environment from Home Assistant. This is a HACS custom integration, not a Home Assistant add-on: it does not install or run T3 Code, and it does not publish activity to T3 Connect. It can connect directly to a local environment or through an already-configured T3 Connect route.

## MVP

- One environment per integration entry, configured in the HA UI.
- Read-only aggregate sensors for session status, pending approvals/input, running turns, and visible threads.
- A connection-health binary sensor.
- Push-based event entity for session creation, approval requests, and user-input requests.
- Read-only aggregate and per-environment usage and estimated-cost history for the current month and rolling 90 days, plus provider-reported quota windows.
- No control or command services.
- Snapshot plus resumable live shell updates; snapshots on initial setup/reconnect are baselines.

T3 Code's shell RPC is currently an in-source client/server contract, not a documented stable third-party API. Compatibility with future T3 versions is not guaranteed. `orchestration:read` cannot mutate T3 state, but it can read files accessible to the T3 server account; protect the integration credentials accordingly.

## Install with HACS

Until this repository is accepted into HACS's default store, add it as a custom repository:

1. In HACS, open **Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/ijtan/ha-t3code-monitor` and choose **Integration** as the category.
3. Install **T3 Code Monitor**, restart Home Assistant, then add it from **Settings → Devices & services**.

Manual installation is also possible: copy `custom_components/t3code` into Home Assistant's `custom_components` directory and restart.

## Pair and configure

On the T3 host, create a one-time pairing credential, for example:

```sh
t3 auth pairing create --label "Home Assistant"
```

Run this for the target environment and copy the credential from the output. In Home Assistant, enter the environment's base URL (for example `http://192.168.1.20:3773` or the base URL of its existing T3 Connect route) and the one-time pairing credential. Do not append `/api` or `/ws`. The host must already be reachable from HA; this integration does not sign in to T3 Connect or create tunnels.

The integration exchanges the one-time credential for a bearer session narrowed to **only** `orchestration:read`. T3 Code currently issues these bearer sessions for 30 days; when one expires, use the integration's **Configure** option with a newly created pairing credential to renew. Pairing credentials are short-lived and single-use. T3's `orchestration:read` scope is still broader than status telemetry because it permits reading files accessible to the server account.

## Entities

Each environment creates count sensors for `starting`, `running`, `ready`, `idle`, `interrupted`, `stopped`, and `error`, plus threads without a session, pending approvals, pending input, running turns, and visible threads. These describe visible, unarchived T3 threads, not operating-system processes. The connection binary sensor reflects whether the integration can reach and synchronize with the environment.

The activity event entity exposes `session_created`, `approval_required`, and `user_input_required` event types. Events are derived from pushed per-thread transitions, not aggregate sensor count changes, and include `environment_id`, `environment_name`, `thread_id`, and `thread_title`. For example, a newly pushed approval request still emits `approval_required` even if other approvals were resolved and the aggregate count fell. Snapshot refreshes and reconnect baselines update sensors but do not synthesize events. Since T3's stream is a coalesced state projection rather than an audit log, transitions missed during a disconnect cannot be replayed as events.

Usage sensors report provider token totals and estimated API-equivalent cost for the current calendar month and rolling 90 days. They refresh every 30 minutes, use the Home Assistant time zone, and combine selected environments while deduplicating matching physical transcript sources. Attributes expose input/cache/output breakdowns, provider totals, pricing quality, and source coverage. This data comes from provider transcript usage on the monitored hosts and can include CLI or other application activity, not just T3 Code sessions. Estimated API-equivalent cost is not a subscription bill; records without known prices are included in token totals and identified as unpriced.

When a T3 Connect entry contains multiple environments, the entry-level sensors remain aggregates and each selected environment also gets its own Home Assistant device with per-environment shell counters, usage totals, provider-limit window count, and connection status. The aggregate connection sensor is on only when every selected environment is reachable; per-environment connection sensors identify which host is unavailable. These per-environment entities are omitted for single-environment entries to avoid duplicating the aggregate sensors.

Provider limit sensors are created when T3 reports quota windows for configured providers or usage-limit sources. Each reports **remaining percent**, with used percent, provider/window, last check, and reset time in attributes. A diagnostic sensor reports how many windows T3 currently exposes and how many environment limit streams are connected. Limit updates use T3's server-config push stream. Limits are provider-reported and provider-dependent; some accounts provide no quota window, and remaining token counts are not available unless the provider itself reports them.

## Limitations

- One-time pairing must currently be created on each T3 host; T3 Connect account OAuth is used for environment discovery only.
- The WebSocket stream is a coalesced state projection, not an audit log. A 30-second snapshot refresh reconciles missed updates; brief intermediate states may not appear.
- Reconnect snapshots reconcile current state and do not replay missed transitions.
- Usage history and quota coverage depend on T3 Code's provider support and accessible transcript sources.

## Connection troubleshooting

The setup form shows the latest connection diagnostic. Home Assistant logs initial setup failures and later connection changes under `custom_components.t3code.coordinator`, with the affected environment name and ID. Repeated reconnect failures are reduced to debug-level messages until the connection recovers. Config-flow and credential renewal issues are logged under `custom_components.t3code.config_flow`. The URL must be the T3 environment's API origin, such as `http://192.168.1.20:3773` or the direct environment hostname provided by T3 Connect. Do not enter `app.t3.codes`, a T3 account page, or a `/pair` URL. Also confirm the HA host itself can resolve and reach that address. Diagnostics intentionally omit credentials, URL paths, and query strings. If setup still fails, share the relevant warning and error lines, but never share the pairing credential or access token.

### T3 Connect account setup

Choose **T3 Connect account** during setup to authorize Home Assistant with T3's OAuth device flow and discover environments already linked to the account. Select one or more environments, then create and enter a one-time pairing credential on each selected T3 host. Home Assistant uses the discovered managed endpoint URL to exchange each credential directly for `orchestration:read`; it does not use the relay DPoP connection exchange. The aggregate sensors sum counts across selected environments. Pairing credentials are single-use, and the resulting access tokens can be renewed from the integration's **Configure** action using newly created pairing credentials. This flow does not create environment links or change T3 settings.

## License

This project is licensed under **GNU GPL version 3 only**. See [LICENSE](LICENSE).
