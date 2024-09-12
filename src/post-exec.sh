#!/bin/sh

# curl on the farm is too old for `--json`
curl -sL -H "Content-Type: application/json" -H "Accept: application/json" \
  --data "{\"job_id\": \"$LSB_JOBID\"}" \
  'http://farm22-cgi-01:8234/job-complete'
