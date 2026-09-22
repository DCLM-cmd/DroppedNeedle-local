# Creating a plugin

Contract reference: [PLUGINS.md](../PLUGINS.md). This guide is the walkthrough;
PLUGINS.md is the authority on every field, method, and limit below.

## Walkthrough

1. Scaffold `my-plugin/plugin.toml` + `my-plugin/plugin.py` (layout: PLUGINS.md "Anatomy").
2. Declare `[plugin]` (`api_version`, `capabilities`) and any v1 tables you need
   (`[[capability]]`, `[schedule]`, `[[route]]`, `[plugin_ui]`). Field rules: PLUGINS.md "Manifest reference".
3. Implement the entrypoint `MyPlugin(context)` with the capability methods you
   declared. Method lists and error semantics: PLUGINS.md "Capabilities".
4. Declare `[[settings]]` for admin-editable values; read them via
   `context.settings`, call out via `context.http`. Secrets handling: PLUGINS.md "Trust model".
5. Install by hand into `<root_app_dir>/plugins`, enable in Settings > Plugins,
   watch the log + health. Install rules: PLUGINS.md "Installing a plugin".
6. Publish by pushing `plugin.toml` + module to a public repo root (PLUGINS.md "Publishing").

## Worked example: catalog revalidator

A minimal `scheduler` plugin that HEADs a user-hosted JSON feed once an hour.
Fictional host only.

`plugin.toml`:

```toml
[plugin]
name = "catalog-watch"
display_name = "Catalog Watch (example)"
version = "1.0.0"
api_version = 1
entrypoint = "plugin:CatalogWatch"
capabilities = ["scheduler"]
description = "Revalidates a user-hosted catalog URL on a schedule."
author = "you"

[schedule]
interval_minutes = 60
run_on_load = false

[[settings]]
key = "catalog_url"
label = "Catalog URL"
help = "A JSON feed you host, e.g. https://example-catalog.test/feed.json"
```

`plugin.py`:

```python
import asyncio


class CatalogWatch:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_tick(self) -> None:
        url = (self.ctx.settings.get("catalog_url") or "").strip()
        if not url:
            return
        try:
            async with asyncio.timeout(30):
                response = await self.ctx.http.head(url, timeout=30.0)
                if response.status_code >= 400:
                    self.ctx.logger.warning("catalog revalidation: HTTP %s", response.status_code)
        except Exception as exc:  # noqa: BLE001 - one bad tick never kills the loop
            self.ctx.logger.warning("catalog revalidation failed: %s", exc)
```

Install it:

```bash
cp -r catalog-watch <data dir>/plugins/
```

Then enable in Settings > Plugins and set `catalog_url` to your feed
(e.g. `https://example-catalog.test/feed.json`). Loop semantics, the 5-minute
floor, and file-write rules: PLUGINS.md "`scheduler`".

Next: add a second capability (e.g. `subscriber` + `publisher` + `[[route]]`)
following the same shape. See `examples/plugins/events-echo-toy` and
PLUGINS.md "Examples".
