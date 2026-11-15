# hagent

hagent is an agent manager for Home Assistant (HA). It runs persistent,
user-defined agents as an HA app. HA automations call the agents. Each agent
keeps its own memory and history, uses tools with scoped permissions, and can
write rules that answer repeated calls at low cost.

Status: first package for local installation. See [the design](docs/design.md).

## Installation

hagent has two parts:

- **App**: the container that runs the agents and the web UI. The HA
  Supervisor builds it from the folder `hagent/` and the sources.
- **Integration**: the HA custom integration in
  `integration/custom_components/hagent`. It connects HA to the app.

Requirements: HA OS or HA Supervised on amd64 (x86-64). The app image
targets amd64 only.

### 1. Install the integration

1. Copy the folder `integration/custom_components/hagent` to
   `/config/custom_components/hagent` on the HA host. Use the Samba share,
   the SSH app or the File editor app.
2. Restart HA (Settings > System > Restart).

### 2. Install the app

The Supervisor builds a local app from a folder in its local apps folder. The
build context is that folder only, so the folder must hold the sources too.
`scripts/package-app.sh` creates it.

1. On your computer, run `scripts/package-app.sh`. The script writes the
   folder `build/app/hagent`.
2. Copy `build/app/hagent` into the local apps folder of the HA host, as
   the folder `hagent`. The Samba share and the SSH app show the local apps
   folder as `addons` (`/addons/hagent`). The Dockerfile sets the executable
   bits again, so a copy over Samba is safe.
3. In HA, open Settings > Apps > App store. Open the menu (three dots) and
   select **Check for updates**. The section "Local apps" shows hagent.
4. Open hagent and select **Install**. The Supervisor builds the image.
   This takes some minutes.
5. Select **Start**. Turn on **Show in sidebar** if you want a side bar
   entry.

To update the app, run the script again, replace the `hagent` folder, raise
`version` in `hagent/config.yaml`, and select **Update** (or **Rebuild**).

### 3. First configuration

1. Open the web UI of the app (**Open web UI** or the side bar entry).
2. On the Settings page, enter an API key for OpenAI or OpenRouter. The
   app stores the key in its database. Do not put keys in a file in the
   repository.
3. On the Settings page, select the approvers. An approver is an HA user
   who can accept or reject a tool call that needs approval.
4. Optional: on the Settings page, select an embedding model for vector
   search in knowledge stores.
5. Connect the integration. When the app starts, it sends its address and
   its link secret to the Supervisor (discovery). HA then shows
   "hagent app found" under Settings > Devices & services. Select
   **Configure**, then **Submit**.

If HA shows no discovery, add the integration by hand: Settings > Devices
& services > Add integration > hagent. Enter:

- Host: `local-hagent` (the host name of a local app).
- Port: `8099`.
- Secret: the `secret` value of `api/integration`. Open the web UI, open
  the frame in a new browser tab, and add `api/integration` to the end of
  its address.

The app options (Configuration tab) hold defaults only. `hagent/DOCS.md`
lists them.

### Backups

HA backups include the data folder of the app (`/data`). It holds the
SQLite database `hagent.db`, the scheduler job store and the link secret.

The app uses `backup: cold`. The Supervisor stops the app, copies `/data`
and starts the app again. The reason: SQLite in WAL mode (write-ahead log)
keeps recent writes in `hagent.db-wal`. A hot backup copies the database
file and the WAL file while the app writes, at different times. The copy
can then be inconsistent. Two other options were rejected:

- `backup_exclude` for `*-wal` and `*-shm` loses the writes that are only
  in the WAL file.
- A `backup_pre` checkpoint does not stop new writes between the
  checkpoint and the copy.

The cost of a cold backup: agents do not run while the backup runs. A
timer that falls due in that time runs after the restart if it is late by
less than `scheduler.missed_run_grace_seconds` (600 seconds).

### Verification checklist

The smoke test (`spikes/task-21/smoke.sh`) runs the image with a fake HA
and no Supervisor. It cannot check the items below. Check them on a real
HA OS installation after the first install.

1. **The app starts.** The Log tab of the app shows
   `hagent installation <ID> started` and `connected to HA <version>`.
2. **Supervisor proxy and HA restart (task-6, AC #1).** Restart HA
   (Settings > System > Restart). The app does not restart. The Log tab
   shows a second `connected to HA` line some seconds after HA is up. The
   web UI shows HA entities again.
3. **The user list (`config/auth/list` with `SUPERVISOR_TOKEN`).** On the
   Settings page, open the approver picker. It lists the HA users. Or open
   `api/ha/users` (as for `api/integration` above): `source` must be
   `auth`. If `source` is `person`, HA refused the command for the
   Supervisor token. The picker then shows only users linked to a person.
4. **The app user (`auth/current_user`).** The Log tab shows the line
   `the app acts as HA user <ID>`. Then let a test agent change a test
   helper (`input_boolean.<test>`). In Developer tools > Template, read
   `{{ states.input_boolean.<test>.context.user_id }}`. It must be the
   same ID as in the log line. The approval gate ignores answers from this
   user, so the two IDs must agree.
5. **Approvals.** Start a tool call that needs approval. The approver gets
   the notification. Accept it. The call runs.
6. **The integration (AC #2).** After the configuration step, the
   integration shows the binary sensor "App link" as on, and one device
   for each agent. Restart the app. The link connects again in less than
   one minute.
7. **Backups (AC #4).** Create a partial backup that holds only the
   hagent app. The app stops during the backup and starts again. Download
   the backup. The archive of the app holds `data/hagent.db`. To test a
   restore, change an agent, restore the backup, and check that the old
   agent version is back.

## Development

Requirements: [mise](https://mise.jdx.dev/).

```bash
mise install      # uv, Node, npm, Backlog.md, pre-commit
mise run setup    # sync Python and front-end dependencies, install git hooks
mise run check    # lint, type check, test (Python and front end)
mise run frontend-build   # build the management UI into frontend/dist
```

Work is tracked with [Backlog.md](https://github.com/MrLesk/Backlog.md) in
`backlog/`. Run `backlog board` to see it.
