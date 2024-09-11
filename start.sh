#!/bin/bash

set -eo pipefail

# go to project folder so we can do al relative paths from now on
cd /nfs/cellgeni/slackbot

# start the app
tmux \
	new-session "uv run --locked farmer-server" \; \
	split-window -h "uv run --locked farmer-reporter"
