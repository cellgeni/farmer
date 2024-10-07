#!/bin/sh

if [ $# -gt 0 ]; then
  export FARMER_SLACK_USER="$1"
fi

you=$FARMER_SLACK_USER
if [ -z "$you" ]; then
  you=you
fi
echo "sending $you a notification that your job finished..."

# don't read input, use compact output
json=$(jq -nc '{job_id: $ENV.LSB_JOBID, array_index: $ENV.LSB_JOBINDEX, user_override: $ENV.FARMER_SLACK_USER}')
# curl on the farm is too old for `--json`
result=$(
  curl -L --fail-with-body \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  --data "$json" \
  'http://farm22-cgi-01.internal.sanger.ac.uk:8234/job-complete'
)
exit=$?

if [ $exit -ne 0 ]; then
  printf 'Could not notify of job completion (%d): %s\n' "$exit" "$result"
fi
