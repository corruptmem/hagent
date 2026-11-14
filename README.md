# hagent

hagent is an agent manager for Home Assistant (HA). It runs persistent,
user-defined agents as an HA app. HA automations call the agents. Each agent
keeps its own memory and history, uses tools with scoped permissions, and can
write rules that answer repeated calls at low cost.

Status: design stage. See [the design](docs/design.md).

## Development

Requirements: [mise](https://mise.jdx.dev/).

```bash
mise install      # uv, Node, Backlog.md, pre-commit
mise run setup    # sync Python dependencies, install git hooks
mise run check    # lint, type check, test
```

Work is tracked with [Backlog.md](https://github.com/MrLesk/Backlog.md) in
`backlog/`. Run `backlog board` to see it.
