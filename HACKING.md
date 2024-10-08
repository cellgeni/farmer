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

(If you have Slack API credentials in a file called `credentials.json` already, you can skip this section.)

You now need to create a Slack app, to get a bot token (`xoxb-...`). Head to https://api.slack.com/apps, and create a
new app from `slack-app-manifest.yaml`, in the workspace you want your bot to live in. Make sure to adjust the app's
appearance (name, bot user) if necessary. You'll find your bot token under OAuth & Permissions ("Bot User OAuth Token").

Additionally, you need to generate an "app-level token" (`xapp-1...-`): visit your app's Basic Information page, scroll
down to "App-Level Tokens", and generate a new token with all three scopes (`connections:write`, `authorizations:read`,
and `app_configurations:write`).

Create a file `credentials.json` containing both tokens:

```json
{
  "SLACK_BOT_TOKEN": "xoxb-...",
  "SLACK_APP_TOKEN": "xapp-1-..."
}
```

### Running the app

Now you can run the app:

```console
$ uv run farmer-reporter
$ uv run farmer-server
```

[uv]: https://docs.astral.sh/uv/
