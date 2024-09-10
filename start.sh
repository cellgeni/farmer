#!/bin/bash

set -eo pipefail

# go to project folder so we can do al relative paths from now on
cd /nfs/cellgeni/slackbot

# start the app
uv run --locked farmer
