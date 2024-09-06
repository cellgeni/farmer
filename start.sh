#!/bin/bash

set -eo pipefail

# go to project folder so we can do al relative paths from now on
cd /nfs/cellgeni/slackbot

# activate environment with dependencies
source env/bin/activate

# start the app
python farmer.py

