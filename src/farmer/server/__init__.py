import asyncio
import json
import logging
import os
import re
from datetime import timedelta

import aiorun
import uvicorn
from fastapi import FastAPI, BackgroundTasks
from fastapi_websocket_rpc import WebsocketRPCEndpoint, RpcChannel
from fastapi_websocket_rpc.rpc_channel import RpcCaller
from pydantic import BaseModel
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.models.blocks import RichTextBlock, RichTextSectionElement, RichTextElementParts, \
    RichTextPreformattedElement, ContextBlock, PlainTextObject, HeaderBlock, MarkdownTextObject, SectionBlock, \
    ButtonElement, DividerBlock, Block, RichTextListElement
from slack_sdk.models.views import View
from slack_sdk.web.async_client import AsyncWebClient

logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")


# When our job-complete notification arrives, the job is still "running"
# (since JOB_INCLUDE_POSTPROC is set). We need to give LSF a bit of time
# to finish running post-execution scripts and circulate the information
# that the job has, in fact, finished.
LSF_CLEANUP_GRACE_SECONDS = 20

# Keys to omit from detailed job info.
LSF_IGNORED_KEYS = {
    "MIN_REQ_PROC",
    "MAX_REQ_PROC",
    "NREQ_SLOT",
    "NALLOC_SLOT",
    "CPU_PEAK",
    "RUNTIMELIMIT",
    "BLOCK",
    "SWAP",
    "EXCEPTION_STATUS",
    "IDLE_FACTOR",
    # Unused things:
    "IMAGE",  # we don't use containers with LSF
    "CONTAINER_NAME",
    "EFFECTIVE_RESREQ",  # resource requirements are very verbose
    "COMBINED_RESREQ",
    "SRCJOBID",  # unused
    "FROM_HOST",  # unused?
    "CTXUSER",  # unused?
    "ESUB",  # we don't use esub(?)
    "JOB_PRIORITY",  # not used
    "EXCLUSIVE",  # not used(?)
    # Uninteresting things:
    "PEND_TIME",  # we already show this
    "FIRST_HOST",  # this is a subset of EXEC_HOSTS
    "USER",  # the user knows who they are!
    "INTERACTIVE",  # the user probably knows this
    "ALLOC_SLOT",  # same as EXEC_HOSTS?
    "USER_GROUP",  # typically uninteresting
    "PROJ_NAME",  # same as USER_GROUP?
    "LONGJOBID",  # same as job ID
    "COMMAND",  # verbose
    "CHARGED_SAAP",  # uninteresting
    "NEXEC_HOST",  # uninteresting
    "EXEC_CWD",
    "EXEC_HOME",
    "SUB_CWD",  # uninteresting?
    "OUTPUT_FILE",  # uninteresting
    "ERROR_FILE",  # uninteresting
    "%COMPLETE",  # uninteresting
    "PIDS",  # should be very rarely needed
    "NTHREADS",
    "RU_STIME",  # not usually interesting for running jobs
    "RU_UTIME",
    "CPU_USED",  # difficult to interpret
    "MEM",  # we already have MAX_MEM and AVG_MEM
    "TIME_LEFT",  # we already have FINISH_TIME
}

# When we handle the last job of a job array, we need to keep track of
# its ID for a few moments so that we don't end up handling it again
# (for example, if two jobs finish simultaneously).
RECENTLY_HANDLED_JOBS = set()


# Shhhhhh: load credentials
# !! please don't commit credentials.json !!
for k, v in json.load(open("credentials.json", mode="rt")).items():
    logging.info(f"Loading '{k}'")
    os.environ[k] = v


# stupidest way to get who you are asking jobs for "jobs for XXXX"
# why people want to know others people's jobs I ask?
# well service accounts for one, nosy people too?
USER_REGEX = re.compile(r"jobs\s+for\s+([\w-]+)", re.IGNORECASE)
def extract_username(message):
    match = re.search(USER_REGEX, message)
    if match:
        return match.group(1)
    return None


def make_job_blocks(job: dict) -> list[Block]:
    # first part is the job id, queue and statues
    job_id = f"{job['JOBID']}[{job['JOBINDEX']}]" if job["JOBINDEX"] != "0" else job["JOBID"]
    text = [f"Job {job_id}: status {job['STAT']}, queue {job['QUEUE']}"]
    if job['STAT'] != "PEND":
        text.append(f"Host(s): {' '.join(set(job['EXEC_HOST'].split(':')))}")
    # second part is when we add all the times submit, start, run
    text.append(f"Submitted {job['SUBMIT_TIME']}")
    if job["STAT"] == "RUN":
        text[-1] += f", started {job['START_TIME']}"
        runtime = timedelta(seconds=int(job['RUN_TIME'].removesuffix(" second(s)")))
        text.append(f"Running for {runtime}")
    # then we talk about efficiency
    if job["STAT"] == "RUN":
        text.append(f"Efficiency: {job['AVERAGE_CPU_EFFICIENCY']} of {job['NALLOC_SLOT']} CPU, {job['MEM_EFFICIENCY']} of {job['MEMLIMIT']} mem")
    # here we should add all the CPU /GPU whatever else we need to show in the summary
    # keep it simple, this is supposed to be succinct, they can click for a job detail action later
    return [
        SectionBlock(
            text=MarkdownTextObject(text="\n".join(text)),
            accessory=ButtonElement(
                text="View Details",
                action_id="job_details",
                value=f"{job['JOBID']}|{job['JOBINDEX']}"
            )
        ),
        DividerBlock(),
    ]


# real init of the slack app with bot token (add socket handler to be explicit?)
slack_bot = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN"))
slack_app_client = AsyncWebClient(os.environ.get("SLACK_APP_TOKEN"))


async def dms_only(message):
    """Filter for messages sent in a DM between two users.

    This is a listener matcher, see the Slack Bolt docs:
    <https://tools.slack.dev/bolt-python/concepts/listener-middleware>
    """
    return message.get("channel_type") == "im"


async def received_by_bot(body):
    """Filter for events received due to a bot authorization.

    When a Slack app is installed, it can receive both its own events
    (e.g. people sending messages to a bot user) and events relating to
    the user who installed the app (e.g. direct messages sent to that
    user). We want to ignore the latter case, so we only get events that
    were received because our own bot user observed them.

    This is a listener matcher, see the Slack Bolt docs:
    <https://tools.slack.dev/bolt-python/concepts/listener-middleware>
    """
    # TODO: we should cache the result of this somewhere
    #   (it's not an urgent problem, since Slack does not ratelimit
    #   auth.test heavily, but still good practice)
    ourself = await slack_bot.client.auth_test()
    auths = body.get("authorizations")
    # this list will contain at most one element
    # https://api.slack.com/changelog/2020-09-15-events-api-truncate-authed-users
    if isinstance(auths, list) and len(auths) == 1 and auths[0].get("user_id") == ourself["user_id"]:
        return True
    # we need to check whether we saw the event for multiple reasons...
    # must use the app token for this API call
    more_auths = await slack_app_client.apps_event_authorizations_list(event_context=body.get("event_context"))
    return any(auth["user_id"] == ourself["user_id"] for auth in more_auths["authorizations"])


@slack_bot.message("(?i)ping", [dms_only, received_by_bot])
async def message_ping(ack, say):
    await ack()
    await say("hello! I am Farmer.")
    cluster = (await rm.reporter.get_cluster_name()).result
    await say(f"I am running on cluster {cluster!r}.")

# ahoy this is handeling the message that has the word JOBS in it
# main use for now...
@slack_bot.message("(?i)jobs", [dms_only, received_by_bot])
async def message_jobs(message, say):
    logging.info(f"message {message}")
    # who's pinging?
    rx = message["blocks"][0]["elements"][0]["elements"][0]["text"]
    logging.info(f"recieved text '{rx}'")
    user = extract_username(rx)
    if not user:
        user = message["user_profile"]["name"]
    # get jobs for user and say it back to them
    jobs = (await rm.reporter.get_jobs(user=user)).result
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
        blocks.extend(make_job_blocks(job))
    await say(blocks=blocks)

# oh fancy you, wanna know more?
# well here are the details for the job you asked for
@slack_bot.action("job_details")
async def handle_job_details(ack, body, logger, client):
    await ack()
    user = body["user"]["username"]
    job_id, array_index = body["actions"][0]["value"].split("|")
    logger.info(f"Username = {user} - JobId = {job_id} - Index = {array_index}")
    await client.chat_postMessage(channel=body["channel"]["id"], text=f"Gathering details about job {job_id}...")
    jobs = (await rm.reporter.get_job_details(job_id=job_id)).result
    # just dump jobs, remove any keys without values, pretty print
    if len(jobs) == 1:
        job = jobs[0]
    else:
        assert array_index
        job = next(j for j in jobs if j["JOBINDEX"] == array_index)
    text = "\n".join(f"{k}: {v}" for k, v in job.items() if (
        bool(v) and v != "-"  # remove things with empty values
        and k not in LSF_IGNORED_KEYS  # remove uninteresting keys
    ))
    await client.chat_postMessage(channel=body["channel"]["id"], text=text)
        # # currently this code is unused
        # # TODO: do we want to condense job arrays into a single list entry by default?
        # blocks = [
        #     HeaderBlock(text=PlainTextObject(text=f"📝 Jobs in array {job_id} (only showing {min(len(jobs), 20)} of {len(jobs)})", emoji=True)),
        # ]
        # for job in jobs[:20]:
        #     blocks.append(make_job_blocks(job))
        # await client.chat_postMessage(channel=body["channel"]["id"], blocks=blocks)
    # how about past jobs? well don't use bjobs we should go to elasticsearch and get you the past info


# sending a message that we don't know?
# tell them we don't know
@slack_bot.event("message", [dms_only, received_by_bot])
async def handle_message_events(body, logger):
    logger.warning("Unknown message")
    logger.warning(body)
    # add "wtf are you talkin about?" response


@slack_bot.event("message")
async def handle_other_message():
    # This handler is needed to silence warnings from slack-bolt.
    pass


@slack_bot.event("app_home_opened")
async def handle_app_home_open(ack, client: AsyncWebClient, event, logger):
    user_id = event["user"]
    logger.info(f"handling app home open for {user_id}")
    await ack()
    user = await client.users_info(user=user_id)
    await client.views_publish(user_id=user_id, view=View(type="home", blocks=[
        HeaderBlock(text=PlainTextObject(text=f"Hello, @{user['user']['name']}!", emoji=False)),
        SectionBlock(text="Head to the “Messages” tab to interact with Farmer."),
        # TODO: can we include a button to take them there?
        # ButtonElement(text="Chat with Farmer", url="slack://app?team={}&id={}&tab=messages"),
        RichTextBlock(elements=[
            RichTextSectionElement(elements=[
                RichTextElementParts.Text(text="Farmer understands the following commands:"),
            ]),
            RichTextListElement(style="bullet", elements=[
                RichTextSectionElement(elements=[
                    RichTextElementParts.Text(text="jobs", style=RichTextElementParts.TextStyle(code=True)),
                    RichTextElementParts.Text(text=": list your currently-running (and pending) jobs"),
                ]),
                RichTextSectionElement(elements=[
                    RichTextElementParts.Text(text="jobs for USERNAME", style=RichTextElementParts.TextStyle(code=True)),
                    RichTextElementParts.Text(text=": list "),
                    RichTextElementParts.Text(text="USERNAME", style=RichTextElementParts.TextStyle(code=True)),
                    RichTextElementParts.Text(text="’s currently-running (and pending) jobs"),
                ]),
            ]),
        ]),
        DividerBlock(),
        RichTextBlock(elements=[
            RichTextSectionElement(elements=[
                RichTextElementParts.Text(text="NB: Farmer is in beta, and may at times be unreliable or even just broken. Please report any bugs to CellGenIT!"),
            ]),
            RichTextSectionElement(elements=[
                RichTextElementParts.Text(text="Farmer is AGPLv3: "),
                RichTextElementParts.Link(text="github.com/cellgeni/farmer", url="https://github.com/cellgeni/farmer")
            ]),
        ]),
    ]))


class ReporterManager:
    def __init__(self):
        self._reporters: list[RpcChannel] = []

    async def on_reporter_connect(self, channel):
        self._reporters.append(channel)

    async def on_reporter_disconnect(self, channel):
        self._reporters.remove(channel)

    @property
    def reporter(self) -> RpcCaller:
        """Get the reporter's RPC caller object.

        Raises an exception if the reporter is not connected.
        """
        return self._reporters[0].other


rm = ReporterManager()
ws_app = FastAPI()
ws_endpoint = WebsocketRPCEndpoint(
    # https://github.com/permitio/fastapi_websocket_rpc/issues/30
    on_connect=[rm.on_reporter_connect],  # type: ignore
    on_disconnect=[rm.on_reporter_disconnect],  # type: ignore
)
ws_endpoint.register_route(ws_app, "/internal/ws")


class JobCompleteNotification(BaseModel):
    job_id: str
    array_index: str | None


@ws_app.post("/job-complete")
async def job_complete(notification: JobCompleteNotification, bg: BackgroundTasks):
    # We want to let the post-exec script finish as soon as possible, so
    # return a response quickly and do the processing later.
    bg.add_task(handle_job_complete, notification)


async def handle_job_complete(notification: JobCompleteNotification):
    # TODO: we should probably check that the job actually used the
    #   post-exec script (to avoid providing an unauthenticated vector
    #   to send confusing messages to the owners of arbitrary jobs)
    # When we're notified, the job will still be in RUN state. So we
    # need to wait a bit for things to quiesce (post-exec scripts, LSF
    # bookkeeping, ...) before we ask what the state of the job is.
    # TODO: if the job is still in RUN state, should we wait longer?
    await asyncio.sleep(LSF_CLEANUP_GRACE_SECONDS)
    if notification.job_id in RECENTLY_HANDLED_JOBS:
        logging.debug("skipping job %r, already handled", notification.job_id)
        return
    try:
        RECENTLY_HANDLED_JOBS.add(notification.job_id)
        jobs = (await rm.reporter.get_job_details(job_id=notification.job_id)).result
        assert jobs
        if len(jobs) == 1:
            await handle_job_complete_inner(jobs[0])
        else:
            all_done = all(j["STAT"] in {"DONE", "EXIT"} for j in jobs)
            # TODO: need to avoid potentially missing out on completed arrays
            #   If the last member of an array is still in RUN state at the time we check it
            #   (that is, almost but not quite finished with post-exec scripts),
            #   then we could fail to notify about that array at all!
            if all_done:
                await handle_job_complete_inner(jobs[0], count=len(jobs))
    finally:
        # TODO: rather than waiting for the remaining tasks to finish,
        #   we should keep track of and proactively cancel them
        try:
            await asyncio.sleep(LSF_CLEANUP_GRACE_SECONDS * 2)
        finally:
            RECENTLY_HANDLED_JOBS.remove(notification.job_id)


async def handle_job_complete_inner(j: dict, count: int = 1):
    username = j["USER"]
    job_id = j["JOBID"]
    cluster = (await rm.reporter.get_cluster_name()).result
    pend_sec = int(j["PEND_TIME"])
    run_sec = int(j["RUN_TIME"].removesuffix(" second(s)"))
    await send_job_complete_message(username=username, job_id=job_id, count=count, cluster=cluster, queue=j["QUEUE"], pend_sec=pend_sec, run_sec=run_sec, cpu_eff=j["AVERAGE_CPU_EFFICIENCY"], mem_eff=j["MEM_EFFICIENCY"], state=j["STAT"], exit_reason=j["EXIT_REASON"], command=j["COMMAND"])


async def send_job_complete_message(*, username: str, job_id: str, count: int, cluster: str, queue: str, pend_sec: int, run_sec: int, cpu_eff: str, mem_eff: str, state: str, exit_reason: str, command: str):
    user = (await slack_bot.client.users_lookupByEmail(email=username + "@sanger.ac.uk"))["user"]
    array = f" (array length {count})" if count > 1 else ""
    match state:
        case "DONE":
            result = "has succeeded"
            result_emoji = "white_check_mark"
        case "EXIT":
            result = "has failed"
            result_emoji = "x"
        case "RUN":
            result = "should be done in a moment"
            result_emoji = "hourglass_flowing_sand"
        case other:
            result = f"reported itself as finished, but is in state {other}"
            result_emoji = "thinking_face"
    reason = f" (reason: {exit_reason!r})" if exit_reason else " "
    # formats like "1 day, 1:02:34"
    pend_time = str(timedelta(seconds=pend_sec))
    run_time = str(timedelta(seconds=run_sec))
    if len(command) <= 200:
        command_desc = "The command was:"
    else:
        command_desc = "The command (truncated due to length) was:"
        command = command[:200]
    # we want to truncate commands based on both byte length *and* line length
    if len(commandlines := command.splitlines(keepends=True)) > 6:
        command_desc = "The command (truncated due to length) was:"
        command = "".join(commandlines[:6])
    footer = "You're receiving this message because your job was configured to use Farmer's post-exec script. For any queries, contact CellGenIT."
    await slack_bot.client.chat_postMessage(
        channel=user["id"],
        text=f"Your job {job_id}{array} on farm {cluster} {result} :{result_emoji}:{reason}"
             + (f"\nIt spent {pend_time} in queue {queue}, then finished in {run_time}.\nEfficiency: {cpu_eff} (CPU), {mem_eff} (mem)." if count == 1 else "")
             + f"\n{command_desc} {command}.\n{footer}",
        blocks=[
            RichTextBlock(elements=[
                RichTextSectionElement(elements=[
                    RichTextElementParts.Text(text="Your job "),
                    RichTextElementParts.Text(
                        text=job_id,
                        style=RichTextElementParts.TextStyle(bold=True),
                    ),
                    RichTextElementParts.Text(text=f"{array} on farm "),
                    RichTextElementParts.Text(
                        text=cluster,
                        style=RichTextElementParts.TextStyle(bold=True),
                    ),
                    RichTextElementParts.Text(text=f" {result} "),
                    RichTextElementParts.Emoji(name=result_emoji),
                    RichTextElementParts.Text(text=reason),
                    # TODO: show stats for job arrays
                    *([
                        RichTextElementParts.Text(text=f"\nIt spent {pend_time} in queue {queue}, then finished in {run_time}."),
                        RichTextElementParts.Text(text=f"\nEfficiency: {cpu_eff} (CPU), {mem_eff} (mem)."),
                    ] if count == 1 else []),
                    RichTextElementParts.Text(text=f"\n{command_desc}"),
                ]),
                RichTextPreformattedElement(elements=[
                    RichTextElementParts.Text(text=command),
                ]),
            ]),
            ContextBlock(elements=[
                PlainTextObject(text=footer, emoji=False),
            ]),
        ],
    )


def serve_uvicorn(server: uvicorn.Server):
    # uvicorn doesn't deal terribly well with being embedded inside a
    # larger application. To ensure a clean shutdown on ctrl-C, we need
    # to start it in a slightly nonstandard way.
    #
    # For context, see <https://github.com/encode/uvicorn/issues/1579>,
    # and the issues and pull requests linked from it.
    async def server_cleanup():
        try:
            await asyncio.Event().wait()
        finally:
            server.should_exit = True
    # Instead of calling a private method, possibly this should be done
    # by subclassing `uvicorn.Server` to make `Server.capture_signals()`
    # a no-op?
    return asyncio.gather(server_cleanup(), aiorun.shutdown_waits_for(server._serve()))


async def async_main():
    server = uvicorn.Server(uvicorn.Config(ws_app, host="0.0.0.0", port=8234, lifespan="off"))
    slack = AsyncSocketModeHandler(slack_bot, os.environ["SLACK_APP_TOKEN"])
    # slack.start_async() would not properly dispose of resources on
    # exit, so we do it by hand...
    await slack.connect_async()
    try:
        await serve_uvicorn(server)
    finally:
        logging.debug("farmer quitting")
        await slack.close_async()


def main():
    aiorun.run(async_main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
