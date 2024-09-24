import asyncio
import json
import logging
import os
import re
import subprocess
import time
from asyncio import CancelledError
from datetime import datetime
from typing import Any, Sequence

import aiorun
from fastapi_websocket_rpc import RpcMethodsBase, WebSocketRpcClient


logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")


# The minimum time between one bjobs invocation ending and the next
# starting, in seconds.
BJOBS_MIN_INTERVAL = 10


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
    _bjobs_worker_instance = None

    def __init__(self) -> None:
        self._cluster_name: str | None = None
        # permit at most this many simultaneous bjobs invocations
        self._bjobs_sem = asyncio.BoundedSemaphore(1)
        self._bjobs_queue: asyncio.Queue[tuple[asyncio.Future, Sequence[str]]] = asyncio.Queue()
        self._last_bjobs_call = time.monotonic_ns()

    async def start(self):
        self._bjobs_worker_instance = asyncio.create_task(self._bjobs_worker())

    async def stop(self):
        await self._bjobs_worker_instance

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

    async def _bjobs(self, *args: str):
        """Run bjobs, returning the parsed JSON result.

        This function does not ratelimit bjobs invocations, so it should
        usually not be called directly. (Otherwise, we could run bjobs
        so frequently that LSF is unable to service other requests.)
        """
        async with self._bjobs_sem:
            # copy the environment because we need the LSF variables to get bjobs info
            bjobs_env = os.environ.copy()
            bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
            proc = await asyncio.create_subprocess_exec(
                "bjobs",
                "-o",
                "all",
                "-json",
                *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=bjobs_env,
            )
            stdout, _ = await proc.communicate()
            self._last_bjobs_call = time.monotonic_ns()
        jobs = json.loads(stdout)
        return jobs

    async def _bjobs_worker(self):
        """Loop forever, handling bjobs requests.

        This worker is responsible for ratelimiting bjobs invocations,
        so there should usually only be a single worker running.
        """
        while True:
            fut, args = await self._bjobs_queue.get()
            now = time.monotonic_ns()
            try:
                waited = (now - self._last_bjobs_call) / 1e9
                assert waited >= 0
                if waited < BJOBS_MIN_INTERVAL:
                    await asyncio.sleep(BJOBS_MIN_INTERVAL - waited)
                result = await self._bjobs(*args)
            except CancelledError:
                fut.cancel()
                self._bjobs_queue.task_done()
                raise
            except Exception as e:
                fut.set_exception(e)
            else:
                fut.set_result(result)
            self._bjobs_queue.task_done()

    async def bjobs(self, *args: str) -> asyncio.Future:
        """Enqueue a bjobs command.

        The returned Future will resolve with the result of bjobs, or
        with an exception or cancellation as appropriate.
        """
        fut = asyncio.get_running_loop().create_future()
        await self._bjobs_queue.put((fut, args))
        return fut

    async def bjobs_for_user(self, user: str):
        """Get bjobs output for a user's currently-running jobs."""
        assert user != "all", "bjobs treats the 'all' user specially"
        fut = await self.bjobs("-u", user)
        return await fut

    async def bjobs_by_id(self, job_id: str):
        """Get bjobs output for a job ID."""
        # TODO: batching
        fut = await self.bjobs(job_id)
        return await fut


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
        logging.info(f"Capturing jobs for {user}")
        jobs = await self.reporter.bjobs_for_user(user)
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
        """Get LSF details for a job ID.

        If the job ID corresponds to an array job, multiple records will
        be returned.
        """
        jobs = await self.reporter.bjobs_by_id(job_id)
        assert jobs["JOBS"] == len(jobs["RECORDS"])
        return jobs["RECORDS"]


async def async_main():
    reporter = FarmerReporter()
    await reporter.start()
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
    await reporter.stop()
    asyncio.get_event_loop().stop()


def main():
    aiorun.run(async_main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
