# hagent

hagent runs persistent AI agents next to Home Assistant (HA). HA automations
call the agents. The hagent integration connects HA to the app.

The installation steps and a verification checklist are in the README of
the project: <https://github.com/corruptmem/hagent#installation>.

## Terms

- **Settings page**: the Settings screen in the hagent web UI. Open the web
  UI from the app page or from the side bar.
- **Option**: a value on the Configuration tab of the app. The Supervisor
  writes the options to `/data/options.json`.

## First configuration

1. Start the app. Open the web UI.
2. On the Settings page, enter an API key for OpenAI or OpenRouter.
3. On the Settings page, select the approvers. An approver is an HA user
   who can accept or reject a tool call that needs approval.
4. Install the hagent integration. HA then shows "hagent app found" under
   Settings > Devices & services. Select **Configure** to connect HA to the
   app.

## Options

The Settings page is the preferred place for each value. A value on the
Settings page replaces the option. The options are defaults only.

| Option | Meaning |
|---|---|
| `log_level` | The level of the app log. |
| `providers.openai_api_key` | Optional. An OpenAI API key. |
| `providers.openrouter_api_key` | Optional. An OpenRouter API key. |
| `providers.agent_daily_budget_usd` | Optional. The daily cost limit of an agent with no own budget. The app default is 0.50 USD. |
| `providers.installation_daily_budget_usd` | Optional. The daily cost limit of all agents together. The app default is 5.00 USD. |
| `providers.genai_prices_auto_update` | Fetch new model prices from GitHub each day. Off by default. |
| `link.discovery` | Send the link address to the Supervisor, so that HA offers the integration set-up. |
| `link.host` | Optional. The host name that the integration connects to. The default is the host name of the app container (`local-hagent` for a local app). |

## Data and backups

The app keeps all data in `/data`: the SQLite database `hagent.db`, the
scheduler job store `scheduler.sqlite` and the link secret `link_secret`.
HA backups include `/data`.

The app uses a cold backup. The Supervisor stops the app during a backup
and starts it again after the backup. Agents do not run during that time.
A timer that falls due during the backup runs after the restart, if the
delay is less than the missed-run grace time (600 seconds by default).
