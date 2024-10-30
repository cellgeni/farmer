# farmer development

## Setup

### Installing dependencies

Install [uv][] (see its docs for up-to-date instructions):

```console
$ curl -LsSf https://astral.sh/uv/install.sh | sh # Linux/macOS
$ powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" # Windows
```

Install the app and its dependencies:

```console
$ uv sync
```

### Creating a Slack app

(If you have Slack API credentials in a file called `.env.server` already, you can skip this section.)

You now need to create a Slack app, to get a bot token (`xoxb-...`). Head to https://api.slack.com/apps, and create a
new app from `slack-app-manifest.yaml`, in the workspace you want your bot to live in. Make sure to adjust the app's
appearance (name, bot user) if necessary. You'll find your bot token under OAuth & Permissions ("Bot User OAuth Token").

Additionally, you need to generate an "app-level token" (`xapp-1...-`): visit your app's Basic Information page, scroll
down to "App-Level Tokens", and generate a new token with all three scopes (`connections:write`, `authorizations:read`,
and `app_configurations:write`).

Create a file `.env.server` containing both tokens:

```shell
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-1-...
```

### Configuring connectivity

Create a file `.env.reporter` containing the URL at which the server will be reachable:

```shell
export FARMER_SERVER_BASE_URL=http://localhost:8080
```

Additionally, set this URL at the top of `src/post-exec.sh` (careful not to commit this change – it may be easier to
change the URL once the file is copied to a well-known location outside the git repository):

```shell
#!/bin/sh

FARMER_SERVER_BASE_URL=http://localhost:8080
```

Add the port the server should run on to your `.env.server` (if you are doing port mapping, this may or may not match
the URL configured above):

```shell
export PORT=8080
```

### Running the app

Now you can run the app:

```console
$ uv run farmer-reporter
$ uv run farmer-server
```

If you want to run Farmer persistently, two sample systemd units are included as `farmer-server.service` and
`farmer-reporter.service`. If running from a source checkout, you may need to prefix `ExecStart` with something like
`/path/to/uv run --locked`, and set `WorkingDirectory=/path/to/checkout`.

[uv]: https://docs.astral.sh/uv/

## Structure

Farmer has two components: the server and the reporter. The server talks to Slack, and handles both requests from users
(asking about their jobs) and notifications of completed jobs (via HTTP request from an LSF post-execution script). The
reporter provides an RPC interface for the server to get details of jobs.

These are separated for several reasons:

- keeping the reporter small makes it easier to audit: it's responsible for rate-limiting access to `bjobs`, and it's
  important to be able to demonstrate that it does this correctly and securely
- the reporter must run on the farm, whereas the server could run anywhere with internet access (e.g. a more isolated
  area of the network)
- in the future, there could be multiple reporters, one per farm

## Avoiding notifications to the wrong users

During development, you can set the environment variable `FARMER_DEV_USER` to your username. Then, Farmer will refuse to
notify any user (and log an error) except that named user.
