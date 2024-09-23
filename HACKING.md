# farmer development

## Setup

Install [uv][] (see its docs for up-to-date instructions):

```console
$ curl -LsSf https://astral.sh/uv/install.sh | sh # Linux/macOS
$ powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" # Windows
```

Install the app and its dependencies:

```console
$ uv sync
```

[uv]: https://docs.astral.sh/uv/
