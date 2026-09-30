# T3 Code Monitor for Home Assistant

Monitor an **already-running** T3 Code environment from Home Assistant. This is a HACS custom integration, not a Home Assistant add-on: it does not install or run T3 Code, and it does not publish activity to T3 Connect. It can connect directly to a local environment or through an already-configured T3 Connect route.

## MVP

- One environment per integration entry, configured in the HA UI.
- Read-only aggregate sensors for session status, pending approvals/input, running turns, and visible threads.
- A connection-health binary sensor.
- No event firing or control/command services in this MVP.
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

## Limitations

- One-time pairing must currently be created on the T3 host; T3 Connect account OAuth is not included.
- The stream is a coalesced state projection, not an audit log. Brief intermediate states may not appear.
- Reconnect snapshots reconcile current state and do not replay missed transitions.
- Usage, cost, quota, and HA events are not included in this MVP.

## Connection troubleshooting

The setup form shows the latest connection diagnostic, and Home Assistant logs a warning under `custom_components.t3code.config_flow`. The URL must be the T3 environment's API origin, such as `http://192.168.1.20:3773` or the direct environment hostname provided by T3 Connect. Do not enter `app.t3.codes`, a T3 account page, or a `/pair` URL. Also confirm the HA host itself can resolve and reach that address. Diagnostics intentionally omit credentials, URL paths, and query strings. If setup still fails, share the warning line and the HTTP status or network error, but never share the pairing credential or access token.

### T3 Connect account setup

Choose **T3 Connect account** during setup to authorize Home Assistant with T3's OAuth device flow, select one or more environments already linked to the account, and connect through their managed relay endpoints. The aggregate sensors sum counts across the selected environments, and the connection health sensor is on only while every selected environment is reachable. Home Assistant stores the refresh credential and DPoP proof key so connections can be renewed from the integration's **Configure** action. This uses the production T3 Connect client configuration and is intended for T3's production relay. It does not create environment links or change T3 settings.

## License

This project is licensed under **GNU GPL version 3 only**. See [LICENSE](LICENSE).
