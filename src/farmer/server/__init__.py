import asyncio
import json
import logging
import os
import re
from datetime import timedelta

import aiorun
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi_websocket_rpc import WebsocketRPCEndpoint, RpcChannel
from fastapi_websocket_rpc.rpc_channel import RpcCaller
from pydantic import BaseModel, model_validator
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.errors import SlackApiError
from slack_sdk.models.blocks import RichTextBlock, RichTextSectionElement, RichTextElementParts, \
    RichTextPreformattedElement, ContextBlock, PlainTextObject, HeaderBlock, MarkdownTextObject, SectionBlock, \
    ButtonElement, DividerBlock, Block, RichTextListElement
from slack_sdk.models.views import View
from slack_sdk.web.async_client import AsyncWebClient
from typing_extensions import Self

from farmer.common import is_dev_mode, get_dev_user
from farmer.server import messaging

try:
    from asyncio import Barrier
except ImportError:  # introduced in Python 3.11
    from farmer.common.barrier import Barrier


load_dotenv(".env.server")
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
        text.append(messaging.format_efficiency(job))
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
matchers = messaging.SlackMatchers(slack_bot.client, slack_app_client)


@slack_bot.message("(?i)ping", [matchers.dms_only, matchers.received_by_bot])
async def message_ping(ack, say):
    await ack()
    ourself = await slack_bot.client.auth_test()
    bot_id = ourself["bot_id"]
    team_id = ourself["team_id"]
    profile = await slack_bot.client.bots_info(bot=bot_id, team_id=team_id)
    name = profile["bot"]["name"]
    await say(f"hello! I am {name}.")
    if dev_user := get_dev_user():
        await say(f":warning: Running in dev mode for user {dev_user}.")
    cluster = (await rm.reporter.get_cluster_name()).result
    await say(f"I am running on cluster {cluster!r}.")

# ahoy this is handeling the message that has the word JOBS in it
# main use for now...
@slack_bot.message("(?i)jobs", [matchers.dms_only, matchers.received_by_bot])
async def message_jobs(message, say, client):
    logging.info(f"message {message}")
    # who's pinging?
    rx = message["blocks"][0]["elements"][0]["elements"][0]["text"]
    logging.info(f"recieved text '{rx}'")
    user = extract_username(rx)
    if not user:
        try:
            user = message["user_profile"]["name"]
        except KeyError:
            # user_profile can be missing if the message is sent from mobile https://github.com/slackapi/bolt-js/issues/2062
            user = (await client.users_info(user=message["user"]))["user"]["name"]
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
@slack_bot.event("message", [matchers.dms_only, matchers.received_by_bot])
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
        HeaderBlock(text=PlainTextObject(text=f":farmer: Hello, @{user['user']['name']}!", emoji=True)),
        SectionBlock(text="Head to the “Messages” tab to interact with Farmer."),
        # TODO: can we include a button to take them there?
        # ButtonElement(text="Chat with Farmer", url="slack://app?team={}&id={}&tab=messages"),
        *([
            SectionBlock(text=PlainTextObject(text=f":warning: This Farmer instance is running in dev mode for user {get_dev_user()} :warning:", emoji=True)),
        ] if is_dev_mode() else []),
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
                RichTextElementParts.Text(text="NB: Farmer is in beta, and may at times be unreliable or even just broken. Please report any bugs to CellGen Informatics!"),
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
    job_id: str | None
    array_index: str | None
    user_override: str | None
    label: str | None

    @model_validator(mode="after")
    def validate(self) -> Self:
        if not self.job_id and not self.user_override:
            raise ValueError("at least one of job_id and user_override must be provided")
        return self


@ws_app.post("/job-complete")
async def job_complete(notification: JobCompleteNotification, bg: BackgroundTasks):
    # We want to let the post-exec script finish as soon as possible, so
    # return a response quickly and do the processing later.
    bg.add_task(handle_job_complete, notification)


BARRIERS: dict[str, Barrier] = {}


async def get_job_barrier(job_id: str, count: int) -> Barrier:
    """Get a barrier for the given job.

    The barrier will block until one party is waiting for each item in
    the job array (or a single party, in the case of a standalone job).
    """
    barrier = BARRIERS.get(job_id)
    if barrier is None:
        BARRIERS[job_id] = barrier = Barrier(count)
    # TODO: consider a more robust guard against job ID reuse
    if barrier.parties != count:
        logging.warning("stale barrier %r for job %r (expected %d, got %d)", barrier, job_id, count, barrier.parties)
        await barrier.abort()
        BARRIERS[job_id] = barrier = Barrier(count)
    return barrier


async def handle_job_complete(notification: JobCompleteNotification):
    # TODO: we should probably check that the job actually used the
    #   post-exec script (to avoid providing an unauthenticated vector
    #   to send confusing messages to the owners of arbitrary jobs)
    if not notification.job_id:
        await send_job_complete_message_simple(username=notification.user_override, label=notification.label)
        return
    # When we're notified, the job will still be in RUN state. So we
    # need to wait a bit for things to quiesce (post-exec scripts, LSF
    # bookkeeping, ...) before we ask it what the state of the job is.
    # (LSF may take longer than this, in which case we will continue to
    # wait, but we expect it to always take at least this long, so
    # there's no point asking sooner.)
    await asyncio.sleep(LSF_CLEANUP_GRACE_SECONDS)
    jobs = (await rm.reporter.get_job_details(job_id=notification.job_id)).result
    assert jobs
    count = len(jobs)
    index = notification.array_index or "0"
    barrier = await get_job_barrier(notification.job_id, count)
    # wait for our job to finish
    while not barrier.broken:
        job = next(j for j in jobs if j["JOBINDEX"] == index)  # TODO: inefficient
        if job["STAT"] in {"DONE", "EXIT"}:
            break
        # this request will be automatically ratelimited and batched
        # TODO: but only on the other end of the RPC link...
        jobs = (await rm.reporter.get_job_details(job_id=notification.job_id)).result
    else:
        logging.error("broken barrier %r while handling notification %r", barrier, notification)
        return
    # wait for any other jobs in the same array to finish
    async with barrier as position:
        # only one notification per job ID!
        if position == 0:
            # this is probably safe, if we assume that we'll get here
            # *reasonably* promptly after the job is done...
            if barrier == BARRIERS[notification.job_id]:
                del BARRIERS[notification.job_id]
            else:
                logging.warning("fast job ID reuse? barrier %r replaced by %r (notification %r)", barrier, BARRIERS[notification.job_id], notification)
            await handle_job_complete_inner(job, count=count, user_override=notification.user_override)


async def handle_job_complete_inner(j: dict, count: int = 1, user_override: str | None = None):
    username = user_override or j["USER"]
    job_id = j["JOBID"]
    cluster = (await rm.reporter.get_cluster_name()).result
    pend_sec = int(j["PEND_TIME"])
    run_sec = int(j["RUN_TIME"].removesuffix(" second(s)"))
    await send_job_complete_message(j, username=username, job_id=job_id, count=count, cluster=cluster, queue=j["QUEUE"], pend_sec=pend_sec, run_sec=run_sec, state=j["STAT"], exit_reason=j["EXIT_REASON"], command=j["COMMAND"])


async def send_job_complete_message(job: dict, *, username: str, job_id: str, count: int, cluster: str, queue: str, pend_sec: int, run_sec: int, state: str, exit_reason: str, command: str):
    if is_dev_mode() and username != (dev_user := get_dev_user()):
        logging.error("refusing to notify other user %r in dev mode for user %r", username, dev_user)
        return
    try:
        user = (await slack_bot.client.users_lookupByEmail(email=username + "@sanger.ac.uk"))["user"]
    except SlackApiError:
        logging.exception("could not infer Slack user from %r (job %r)", username, job_id)
        return
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
    efficiency = messaging.format_efficiency(job)
    if len(command) <= 200:
        command_desc = "The command was:"
    else:
        command_desc = "The command (truncated due to length) was:"
        command = command[:200]
    # we want to truncate commands based on both byte length *and* line length
    if len(commandlines := command.splitlines(keepends=True)) > 6:
        command_desc = "The command (truncated due to length) was:"
        command = "".join(commandlines[:6])
    footer = "You're receiving this message because your job was configured to use Farmer's post-exec script. For any queries, contact CellGen Informatics."
    await slack_bot.client.chat_postMessage(
        channel=user["id"],
        text=f"Your job {job_id}{array} on farm {cluster} {result} :{result_emoji}:{reason}"
             + (f"\nIt spent {pend_time} in queue {queue}, then finished in {run_time}.\n{efficiency}." if count == 1 else "")
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
                        RichTextElementParts.Text(text=f"\n{efficiency}."),
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


async def send_job_complete_message_simple(username: str, label: str | None):
    if is_dev_mode() and username != (dev_user := get_dev_user()):
        logging.error("refusing to notify other user %r in dev mode for user %r", username, dev_user)
        return
    try:
        user = (await slack_bot.client.users_lookupByEmail(email=username + "@sanger.ac.uk"))["user"]
    except KeyError:
        logging.error("could not infer Slack user from %r (label %r)", username, label)
        return
    label = label or "(no label provided)"
    footer = "You're receiving this message because you used Farmer's notification hook. For any queries, contact CellGen Informatics."
    await slack_bot.client.chat_postMessage(
        channel=user["id"],
        text=f"Your job {label} has finished. Since it wasn't submitted via LSF, no details are available.\n{footer}",
        blocks=[
            RichTextBlock(elements=[
                RichTextSectionElement(elements=[
                    RichTextElementParts.Text(text="Your job "),
                    RichTextElementParts.Text(
                        text=label,
                        style=RichTextElementParts.TextStyle(bold=True),
                    ),
                    RichTextElementParts.Text(text=" has finished. Since it wasn't submitted via LSF, no details are available."),
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
    server = uvicorn.Server(uvicorn.Config(ws_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8234)), lifespan="off"))
    slack = AsyncSocketModeHandler(slack_bot, os.environ["SLACK_APP_TOKEN"])
    # slack.start_async() would not properly dispose of resources on
    # exit, so we do it by hand...
    await slack.connect_async()
    try:
        await serve_uvicorn(server)
    finally:
        logging.debug("farmer quitting")
        await slack.close_async()
    asyncio.get_running_loop().stop()


def main():
    aiorun.run(async_main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
