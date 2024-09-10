import json
import logging
import os
import re
import shlex
import textwrap
from datetime import datetime
from subprocess import PIPE, run

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# setup loggggggg
logging.basicConfig(level="DEBUG", format="[%(asctime)s][%(levelname)s] %(message)s")

# Shhhhhh: load credentials
# !! please don't commit credentials.json !!
for k, v in json.load(open("credentials.json", mode="rt")).items():
    logging.info(f"Loading '{k}'")
    os.environ[k] = v


# this is used to convert stupid LSF dates to python dates
DATE_REGEX = re.compile(
    r"([A-Z]{1}[a-z]{2})(?:\s+)(\d{1,2}) (\d{2}:\d{2}:\d{2}) (\d{4})(?:\s?)"
)
def lsf_date_to_datetime(lsftime):
    try:
        matches = re.search(DATE_REGEX, lsftime)
        b = matches.group(1)
        d = matches.group(2).rjust(2, "0")
        HMS = matches.group(3)
        Y = matches.group(4)
        return datetime.strptime(f"{b} {d} {HMS} {Y}", "%b %d %H:%M:%S %Y")
    except Exception as e:
        logging.error(f"lsf_date_to_datetime {e}")
        return None


def get_jobs(user: str) -> list:
    # sanitize because 1337 hax0rdz may be around?
    user = shlex.quote(user)
    # clock in whend id we get the info
    timestamp = datetime.now().isoformat(sep="T", timespec="seconds")
    logging.info(f"Capturing jobs at {timestamp} for {user}")
    # get jobs — we copy the environment because we need al the LSF crap to get bjobs info
    bjobs_env = os.environ.copy()
    bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
    command = f"bjobs -u {user} -json -o 'all'"
    # run the actual capture of the jobs
    logging.debug(command)
    result = run(
        command,
        stdout=PIPE,
        stderr=PIPE,
        env=bjobs_env,
        universal_newlines=True,
        shell=True,
    )
    # parse jobs
    jobs = json.loads(result.stdout)
    jobs = jobs["RECORDS"]
    # sort by status and id because I'm nice like that
    jobs = sorted(jobs, key=lambda j: (j['STAT'],j['JOBID']))
    # convert dates to python datetime, why? don't know to iso serialize them latter on — maybe?
    for idx, job in enumerate(jobs):
        for time_field in ["SUBMIT_TIME", "START_TIME", "FINISH_TIME"]:
            try:
                jobs[idx][time_field] = lsf_date_to_datetime(job[time_field])
            except:
                pass
    # build response blocks
    # header first "You haz jobs"
    blocks = [
        {
            "type": "header",
            "text": {"emoji": True, "text": f"📝 Jobs for {user} (only showing {min(len(jobs),20)} of {len(jobs)})", "type": "plain_text"},
        }
    ]
    # sections latter (top 20 because there is a slack limit FML)
    # also "sections" (aka tables) in slack are horrid
    # sorry for you future me trying to style that
    for job in jobs[:20]:
        # first part is the job id, queue and statues
        text = [ f"JOBID={job['JOBID']} | STAT={job['STAT']} | QUEUE={job['QUEUE']}" ]
        if job['STAT']!="PEND":
            text.append( f"EXEC_HOST={' '.join(set(job['EXEC_HOST'].split(':')))}" )
        # second part is when we add all the times submit, start, run
        times = [ f"SUBMIT_TIME={job['SUBMIT_TIME']}" ]
        if job["STAT"]=="RUN":
            times.append( f"START_TIME={job['START_TIME']}" )
            runtime = int(job['RUN_TIME'].replace(" second(s)",""))//60
            times.append( f"RUN_TIME={runtime}min" )
        text.append( " | ".join(times) )
        # then we add all the memory
        mem = [ f"MEMLIMIT={job['MEMLIMIT']}" ]
        if job["STAT"]=="RUN":
            mem.append( f"AVG_MEM={job['AVG_MEM']}" )
            mem.append( f"MEM_EFFICIENCY={job['MEM_EFFICIENCY']}" )
        text.append( " | ".join(mem).replace("bytes","") )
        # here we should add all the CPU /GPU whatever else we need to show in the summary
        # keep it simple, this is supposed to be succinct, they can click for a job detail action later
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": '\n'.join(text),
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Details"},
                    "action_id": "job_details",
                    "value": job["JOBID"],
                },
            }
        )
        blocks.append({"type": "divider"})

    return blocks


# stupidest way to get who you are asking jobs for "jobs for XXXX"
# why people want to know others people's jobs I ask? 
# well service accounts for one, nosy people too?
USER_REGEX = re.compile(r"jobs\s+for\s+([\w-]+)", re.IGNORECASE)
def extract_username(message):
    match = re.search(USER_REGEX, message)
    if match:
        return match.group(1)
    return None

# real init of the slack app with bot token (add socket handler to be explicit?)
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# ahoy this is handeling the message that has the word JOBS in it
# main use for now...
@app.message("jobs")
def message_jobs(message, say):
    logging.info(f"message {message}")
    # who's pinging?
    rx = message["blocks"][0]["elements"][0]["elements"][0]["text"]
    logging.info(f"recieved text '{rx}'")
    user = extract_username(rx)
    if not user:
        user = message["user_profile"]["name"]
    # get jobs for user and say it back to them
    response_block = get_jobs(user)
    say(blocks=response_block)

# oh fancy you, wanna know more?
# well here are the details for the job you asked for
@app.action("job_details")
def handle_job_details(ack, body, logger, client):
    ack()
    user = body["user"]["username"]
    jobid = body["actions"][0]["value"]
    logger.info(f"Username = {user} - JobId = {jobid}")
    bjobs_env = os.environ.copy()
    bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
    command = f"bjobs -json -o 'all' {jobid}"
    logging.debug(command)
    result = run(
        command,
        stdout=PIPE,
        stderr=PIPE,
        env=bjobs_env,
        universal_newlines=True,
        shell=True,
    )
    # parse jobs
    jobs = json.loads(result.stdout)
    jobs = jobs["RECORDS"][0]
    client.chat_postMessage(channel=body["channel"]["id"], text=f"I hear you. You wanna know more about JOBID={jobid}")
    # just dump jobs, remove any keys without values, pretty print
    client.chat_postMessage(channel=body["channel"]["id"], text=json.dumps({k: v for k, v in jobs.items() if v}, indent=4) )
    # how about past jobs? well don't use bjobs we should go to elasticsearch and get you the past info

# sending a message that we don't know?
# tell them we don't know
@app.event("message")
def handle_message_events(body, logger):
    logger.warning("Unknown message")
    logger.warning(body)
    # add "wtf are you talkin about?" response


# entrypoint of the script that actually invokes the app start in socket mode
def main():
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
