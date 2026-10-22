# OMP Session Hub

A lightweight phone-friendly front door for live [OMP](https://github.com/can1357/oh-my-pi) sessions and [Herdr](https://herdr.dev) workspaces. Python 3.9 or newer, with no third-party dependencies.

- List live collaboration sessions across projects, with activity and input-required status.
- Open OMP's native browser conversation with the access published by its host.
- Close a live session's Herdr pane, with confirmation and the same scope as `prefix+x`.
- Create a fresh or shared task workspace, clone a repository, or add an OMP tab to an existing workspace through a configured launcher adapter.
- Reach an IDE-forwarded CDE dashboard from a phone using a small Mac LAN gateway.

Saved conversations and stopped sessions are not listed. The hub is a launcher, not another chat client. Conversation traffic still uses OMP's end-to-end-encrypted collaboration relay.

## Closing a session

**Close session** stops the selected live agent by closing its Herdr pane, not by deleting its saved OMP conversation. It leaves sibling panes, including an editor split, open. If it is the last pane, Herdr closes the containing tab or workspace and its normal checkout cleanup runs. The confirmation shows the current impact and warns when the agent is working.

Only control-enabled collaboration hosts with an exact, foreground Herdr match can be closed. Standalone sessions, ambiguous matches, and stale confirmations are refused. The hub never guesses a pane from a title or working directory, kills an arbitrary PID, or broadens a failed pane close to a workspace close. A lost response is not retried automatically; refresh and inspect Herdr before another close.

The adapter rechecks the collaboration generation, saved-session identity, foreground PID, terminal identity, and layout immediately before invoking native pane close. Herdr does not offer an atomic expected-session condition for that operation, so concurrent external pane replacement cannot be serialized by the hub.

## Trust boundary

**There is no login. Anyone who can reach the gateway can read sessions, obtain agent-control links, and create workspaces.** Run it only on a trusted private network. Do not publish it through router forwarding, a public tunnel, or a public CDE preview.

The CDE hub binds to `127.0.0.1`. The Mac gateway binds to its selected private LAN IPv4 address, not `0.0.0.0`. It proxies only to a fixed Mac loopback port. Host and Origin checks and a required JSON request marker reject browser cross-site mutations and DNS-rebinding requests. They do not authenticate other devices on the same LAN. A hostile local process can also access the hub.

The gateway serves HTTP. Its phone-to-Mac LAN traffic is not protected by application TLS; network reachability is the chosen permission boundary, not a guarantee of confidentiality. Use a separately configured trusted HTTPS reverse proxy or a private encrypted network when that is required. Control URLs are bearer secrets. The application does not log request paths, bodies, or URLs, and responses are not cacheable.

## Install

Install an exact public Git commit rather than a moving branch:

```sh
REVISION=<full-40-character-commit>
curl -fsSL "https://raw.githubusercontent.com/YanxiChen-gh/omp-session-hub/$REVISION/install.sh" \
  -o /tmp/install-omp-session-hub.sh
sh /tmp/install-omp-session-hub.sh --revision "$REVISION"
```

An optional `--sha256 <archive-digest>` verifies the downloaded source archive. To install an already downloaded checkout, use `--source /absolute/source/path --revision "$REVISION"`. `--prefix` defaults to `~/.local/share/omp-session-hub`. Installations are immutable revision directories; `~/.local/bin/omp-session-hub` points to the selected Python zip application. Existing non-symlink user files are not replaced. Put `~/.local/bin` on PATH.

[Dotfiles](https://github.com/YanxiChen-gh/Dotfiles) supplies a pinned, checksum-verified installation and the personal Herdr/Treehouse adapter.

## Run in the CDE

OMP must support `collab.autoStart` and `omp collab list/link --json`. Configure automatic full-control collaboration for future interactive sessions:

```sh
omp config set collab.autoStart control
```

For an already running session, run `/collab` once. Auto-start does not grant tool auto-approval. The host's existing tool permissions still apply. A room rotates when its session changes; opening a stale dashboard selection is rejected instead of connecting to its replacement.

Start the hub with your workspace adapter:

```sh
omp-session-hub hub --port 4317 --adapter /absolute/path/to/session-hub-adapter.py
```

Forward CDE port **4317** to Mac localhost port **4317** using your IDE. Keep the local port fixed. A different CDE should not replace this mapping while a phone is using it. Multiple OMP sessions inside one CDE use the same mapping.

The hub polls OMP's private local registry. `Idle` means no turn is currently running, not that all asynchronous work has completed. View-only hosts open read-only; configure `control` for phone interaction.

## Run on the Mac

```sh
omp-session-hub gateway --port 8787 --upstream-port 4317
```

The gateway prints its selected private LAN address. Open that address on the phone while both devices can reach each other. Automatic binding follows the Mac's selected LAN interface and rebinds when its IPv4 address changes. It never opens a second SSH tunnel. The CDE application and IDE forward remain responsible for the upstream connection.

For an explicit private interface, pass `--bind <private-ipv4>`. Loopback binding is available for local verification. A disconnected forward returns an unavailable response. The Mac must remain awake, and the IDE forward and CDE must remain running. Guest Wi-Fi client isolation can prevent access even on the same Wi-Fi.

## Background services

```sh
# In the CDE:
omp-session-hub service install hub --port 4317 --adapter /absolute/path/to/session-hub-adapter.py
omp-session-hub service ensure hub
omp-session-hub service status hub

# On the Mac:
omp-session-hub service install gateway --port 8787 --upstream-port 4317
omp-session-hub service status gateway
```

Mac services use a per-user LaunchAgent with `RunAtLoad` and `KeepAlive`. Linux services use a locked per-user supervisor that restarts a failed child; invoke `service ensure hub` from the CDE startup workflow to restore it after an environment restart. Neither service keeps a stopped CDE or sleeping Mac alive. `service stop hub` and `service stop gateway` stop only their managed service. Services retain their installed configuration when ensured without arguments.

Both listeners bind numeric addresses without reverse-DNS lookup, so DNS availability does not delay startup. Private service state and bounded logs live under `${XDG_STATE_HOME:-~/.local/state}/omp-session-hub`. On the Mac, `gateway.log` records the selected phone-facing URL and address changes.

## Workspace adapter

The adapter is a local Python file selected by the operator, never by an HTTP request. The hub invokes it with the same Python interpreter:

- `adapter.py inventory`: emit JSON with `repositories` and `workspaces` arrays. Entries have `id`, `name`, and `path` or `cwd`.
- `adapter.py create`: read a JSON object from stdin and emit a ready result `{ "workspaceId": "w1", "paneId": "w1:p1", "cwd": "/checkout" }` or `{ "error": "safe user-facing explanation" }`.
- `adapter.py close-preview`: resolve a trusted collaboration host to its exact live pane and return a confirmation target, including whether its tab or workspace would close.
- `adapter.py close`: revalidate the same host and confirmation target, then close only that pane. Errors may include a numeric HTTP `status` alongside the safe `error`.

Creation accepts `repository`, `checkout` (`fresh` or `primary`), `agent`, `editor`, and `prompt`. `workspaceId` instead selects an existing workspace for a new OMP tab. `cloneRepository` and optional `cloneRoot` instead request a GitHub checkout. Unknown options and invalid types are rejected. The adapter owns path validation, repository policy, safe argv construction, readiness, and preserving checkout lifetime after a request returns.

Creation runs asynchronously, with at most four pending jobs and 64 retained statuses. Status is in memory, not a durable job database. After a timeout, lost response, or hub restart, inspect Herdr before resubmitting: the workspace may already exist. Closing an existing workspace occurs only as Herdr's normal consequence of a confirmed last-pane close. The hub does not resume or delete historical sessions.

## Development

```sh
python3 -m omp_hub hub --port 4317 --adapter /absolute/path/to/adapter.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q omp_hub
sh -n install.sh
```

CI runs the behavior suite on Linux with Python 3.9 and 3.12, and macOS with Python 3.12. The tests cover request boundaries, session-generation races, creation failures, proxy behavior, and service lifecycle, including native launchd when a GUI domain is available. A local desktop test does not establish phone reachability or IDE forwarding; verify those on the actual Mac and phone before relying on LAN access.
