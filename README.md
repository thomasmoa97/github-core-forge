# github-core-forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/thomasmoa97/github-core-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/thomasmoa97/github-core-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone GitHub-compatible git forge in one Python 3.13 process, standard
library only. It serves git smart HTTP (upload-pack protocol v2, receive-pack
v0), the REST v3 API, GraphQL v4, and MCP over Streamable HTTP — all from its
own in-memory store, with no git binary, no UI, and no outside service.

## Run it

```sh
python3 main.py
```

In a trial the process binds an ephemeral port, registers it with
`$MAKEFUN_ENV_URL/register`, polls `$MAKEFUN_ENV_URL/done`, and exits 0.
Locally (no `MAKEFUN_ENV_URL`) it serves on `FORGE_PORT` (default 18080).
`MAKEFUN_SHOP_TOKEN` is the bearer token every API call must present.

## Layout

- `main.py` — entry point and trial lifecycle.
- `forge/httpapi.py` — threaded HTTP server, routing, auth, JSON envelope.
- `forge/store.py` — repositories, refs, issues, pull requests, merges.
- `forge/gitobj.py` — git objects, packfiles, deltas, reachability.
- `forge/gitproto.py` — git smart HTTP (upload-pack v2, receive-pack v0).
- `forge/rest.py` — REST v3 API.
- `forge/graphql.py` — GraphQL v4 parser and resolvers.
- `forge/mcp.py` — MCP (legacy handshake, tools/list, tools/call).
