#!/bin/sh

FARMER_SERVER_BASE_URL=http://farm22-cgi-01.internal.sanger.ac.uk:8234

# Usage: post-exec.sh [--user=USER] [--] [USER]
#
# Options:
#   --user=USER: a user to notify (if unspecified, use job owner)
# Positional arguments:
#   USER: a user to notify (if unspecified, use job owner)

export FARMER_WARNINGS=
warn() {
  printf 'warning: %s\n' "$1"
  FARMER_WARNINGS="$FARMER_WARNINGS${FARMER_WARNINGS:+; }$1"
}

pos=0
handle_positional() {
  : "$(( pos = pos + 1 ))"
  case $pos in
    1)
      export FARMER_SLACK_USER="$1"
      ;;
    *)
      warn "unrecognised positional argument: $1"
      ;;
  esac
}

# handle positional arguments and options
while [ $# -gt 0 ]; do
  arg=$1
  shift
  case $arg in
    --user)
      if [ $# -gt 0 ]; then
        param=$1
        shift
        export FARMER_SLACK_USER="$param"
      else
        warn "missing parameter for $arg"
      fi
      ;;
    --user=*)
      export FARMER_SLACK_USER="${arg#--user=}"
      ;;
    --label)
      if [ $# -gt 0 ]; then
        param=$1
        shift
        export FARMER_LABEL="$param"
      else
        warn "missing parameter for $arg"
      fi
      ;;
    --label=*)
      export FARMER_LABEL="${arg#--label=}"
      ;;
    --)
      break
      ;;
    -*)
      warn "unrecognised option: ${arg#*=}"
      ;;
    *)
      handle_positional "$arg"
      ;;
  esac
done
# handle positional arguments only
while [ $# -gt 0 ]; do
  arg=$1
  shift
  handle_positional "$arg"
done

you=$FARMER_SLACK_USER
if [ -z "$you" ]; then
  you=you
fi
echo "sending $you a notification that your job finished..."

# don't read input, use compact output
json=$(jq -nc '{job_id: $ENV.LSB_JOBID, array_index: $ENV.LSB_JOBINDEX, user_override: $ENV.FARMER_SLACK_USER, label: $ENV.FARMER_LABEL}')
# curl on the farm is too old for `--json`
result=$(
  curl -L --fail-with-body \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  --data "$json" \
  "$FARMER_SERVER_BASE_URL/job-complete"
)
exit=$?

if [ $exit -ne 0 ]; then
  printf 'Could not notify of job completion (%d): %s\n' "$exit" "$result"
fi
