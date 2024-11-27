#!/bin/sh

FARMER_SERVER_BASE_URL=http://farm22-cgi-01.internal.sanger.ac.uk:8234

# Usage: post-exec.sh [--user=USER] [--] [USER]
#
# Options:
#   --user=USER: a user to notify (if unspecified, use job owner)
#   --label=LABEL: an arbitrary string to help identify your job
#   --payload=PAYLOAD: an arbitrary string, like label (displayed in monospace)
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

# Handle option parameters in "separated" form (`--option param`).
# Calls should usually be followed by `&& shift`.
handle_opt_param_sep() {
  varname=$1
  optname=$2
  value=$3
  if [ $# -gt 0 ]; then
    export "$varname"="$value"
  else
    warn "missing parameter for $optname"
    return 1
  fi
}

# Handle option parameters in "joined" form (`--option=param`).
handle_opt_param_join() {
  varname=$1
  optname=$2
  export "$varname"="${arg#"$optname"=}"
}

# handle positional arguments and options
while [ $# -gt 0 ]; do
  arg=$1
  shift
  case $arg in
    --user)
      handle_opt_param_sep FARMER_SLACK_USER --user "$1" && shift
      ;;
    --user=*)
      handle_opt_param_join FARMER_SLACK_USER --user
      ;;
    --label)
      handle_opt_param_sep FARMER_LABEL --label "$1" && shift
      ;;
    --label=*)
      handle_opt_param_join FARMER_LABEL --label
      ;;
    --payload)
      handle_opt_param_sep FARMER_PAYLOAD --payload "$1" && shift
      ;;
    --payload=*)
      handle_opt_param_join FARMER_PAYLOAD --payload
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
json=$(jq -nc '{job_id: $ENV.LSB_JOBID, array_index: $ENV.LSB_JOBINDEX, user_override: $ENV.FARMER_SLACK_USER, label: $ENV.FARMER_LABEL, payload: $ENV.FARMER_PAYLOAD}')
# curl on the farm is too old for `--json`
result=$(
  curl -L --no-progress-meter --fail-with-body \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  --data "$json" \
  "$FARMER_SERVER_BASE_URL/job-complete"
)
exit=$?

if [ $exit -ne 0 ]; then
  printf 'Could not notify of job completion (%d): %s\n' "$exit" "$result"
fi
