import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
from datetime import datetime
from typing import Any

import aiorun
from fastapi_websocket_rpc import RpcMethodsBase, WebSocketRpcClient


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


# NB:
# - RPC calls may only use keyword arguments, so make the methods keyword-only for clarity
# - return type annotations are mandatory
class FarmerReporter(RpcMethodsBase):
    async def get_jobs(self, *, user: str) -> Any:
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
        # TODO: make async
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        return jobs

    async def get_job_details(self, *, job_id: str) -> Any:
        bjobs_env = os.environ.copy()
        bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
        command = f"bjobs -json -o 'all' {job_id}"
        logging.debug(command)
        # TODO: make async
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=bjobs_env,
            universal_newlines=True,
            shell=True,
        )
        # parse jobs
        jobs = json.loads(result.stdout)
        job = jobs["RECORDS"][0]
        return job


async def async_main():
    # TODO: retry logic?
    # (there should be some built into fastapi_websocket_rpc)
    disconnected = asyncio.Event()
    async def on_disconnect(channel):
        disconnected.set()
    async with WebSocketRpcClient(
            "ws://localhost:8234/internal/ws",
            FarmerReporter(),
            on_disconnect=[on_disconnect],
    ):
        await disconnected.wait()
    asyncio.get_event_loop().stop()


def main():
    logging.basicConfig(level="DEBUG", format="[%(asctime)s][%(levelname)s] %(message)s")
    aiorun.run(async_main(), stop_on_unhandled_errors=True)
