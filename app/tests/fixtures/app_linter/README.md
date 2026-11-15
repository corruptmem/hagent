# App linter schema

`config.schema.json` is a copy of `src/config.schema.json` from the HA app
linter (`frenck/action-app-linter`), commit
`d51677290aef26cdfc835271dd3c7343ce502809`.

`test_app_package.py` validates `hagent/config.yaml` against it, with the
same rule as the linter: a key that has its default value is an error.

The linter uses the MIT licence. `LICENSE.md` is its licence text.

To update the copy, download the file from the linter repository and
change the commit above.
