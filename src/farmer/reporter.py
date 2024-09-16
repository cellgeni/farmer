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


logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")


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


class FarmerReporter:
    def __init__(self) -> None:
        self._cluster_name: str | None = None

    async def get_cluster_name(self) -> str:
        """Gets the name of the current LSF cluster."""
        if self._cluster_name:
            return self._cluster_name
        # We should really use pythonlsf, but IBM also do it this way:
        # <https://github.com/IBMSpectrumComputing/lsf-utils/blob/fe9ba1ddf9897d9e36899c3b8d671cf7ea979bdf/bsubmit/bsubmit.cpp#L38>
        proc = await asyncio.create_subprocess_exec(
            "lsid",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, proc.returncode
        assert len(stderr) == 0, stderr
        prefix = b"My cluster name is "
        for line in stdout.splitlines():
            if line.startswith(prefix):
                self._cluster_name = line[len(prefix):].decode()
                return self._cluster_name
        assert False, f"lsid failed: {stdout}"


# NB:
# - RPC calls may only use keyword arguments, so make the methods keyword-only for clarity
# - return type annotations are mandatory
class FarmerReporterRpc(RpcMethodsBase):
    def __init__(self, reporter: FarmerReporter) -> None:
        super().__init__()
        self.reporter = reporter

    async def get_cluster_name(self) -> str:
        """Gets the name of the current LSF cluster."""
        return await self.reporter.get_cluster_name()

    async def get_jobs(self, *, user: str) -> Any:
        # sanitize because 1337 hax0rdz may be around?
        user = shlex.quote(user)
        # clock in whend id we get the info
        timestamp = datetime.now().isoformat(sep="T", timespec="seconds")
        logging.info(f"Capturing jobs at {timestamp} for {user}")
        # get jobs — we copy the environment because we need al the LSF crap to get bjobs info
        bjobs_env = os.environ.copy()
        bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
        # run the actual capture of the jobs
        proc = await asyncio.create_subprocess_exec(
            "bjobs",
            "-u",
            user,
            "-o",
            "all",
            "-json",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=bjobs_env,
        )
        stdout, _ = await proc.communicate()
        # parse jobs
        jobs = json.loads(stdout)
        jobs = jobs["RECORDS"]
        # sort by status and id because I'm nice like that
        jobs = sorted(jobs, key=lambda j: (j['STAT'],j['JOBID']))
        # convert dates to python datetime, why? don't know to iso serialize them latter on — maybe?
        for idx, job in enumerate(jobs):
            for time_field in ["SUBMIT_TIME", "START_TIME", "FINISH_TIME"]:
                try:
                    jobs[idx][time_field] = lsf_date_to_datetime(job[time_field])
                except Exception:
                    pass
        return jobs

    async def get_job_details(self, *, job_id: str) -> Any:
        bjobs_env = os.environ.copy()
        bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
        proc = await asyncio.create_subprocess_exec(
            "bjobs",
            "-json",
            "-o",
            "all",
            job_id,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=bjobs_env,
        )
        stdout, _ = await proc.communicate()
        # parse jobs
        jobs = json.loads(stdout)
        job = jobs["RECORDS"][0]
        return job


async def async_main():
    reporter = FarmerReporter()
    # TODO: retry logic?
    # (there should be some built into fastapi_websocket_rpc)
    disconnected = asyncio.Event()
    async def on_disconnect(channel):
        disconnected.set()
    async with WebSocketRpcClient(
            "ws://localhost:8234/internal/ws",
            FarmerReporterRpc(reporter),
            on_disconnect=[on_disconnect],
    ):
        await disconnected.wait()
    asyncio.get_event_loop().stop()


def main():
    aiorun.run(async_main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
