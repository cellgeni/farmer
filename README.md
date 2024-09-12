# farmer

Slack bot that tells you the status of your LSF jobs

## Usage

To be notified when jobs finish, you can use the included post-exec script:

```console
$ bsub ... -Ep /path/to/farmer/src/post-exec.sh
```

## Development setup

Install [uv][]:

```console
$ curl -LsSf https://astral.sh/uv/install.sh | sh # Linux/macOS
$ powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" # Windows
```

Install the app and its dependencies:

```console
$ uv sync
```

[uv]: https://docs.astral.sh/uv/
