import asyncio
import contextlib
import enum
import json
import logging
import os
import re
import ssl
import subprocess
import time
import urllib.parse
from abc import abstractmethod, ABC
from asyncio import CancelledError
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence, Literal, Callable, Awaitable
from urllib.parse import urlunparse

import aiorun
from dotenv import load_dotenv
from fastapi_websocket_rpc import RpcMethodsBase, WebSocketRpcClient


load_dotenv(".env.reporter")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")


# The minimum time between one bjobs invocation ending and the next
# starting, in seconds.
BJOBS_MIN_INTERVAL = 60

# The minimum time to wait before trying again if `bjobs` exits with a
# nonzero exit code. This may indicate problems with the cluster.
BJOBS_FAILURE_BACKOFF = 60

# The minimum time to wait before trying again if `lsid` fails to tell
# us the cluster name, in seconds. This should be extremely rare unless
# there are problems with the cluster.
LSID_BACKOFF = 60


class BjobsError(Exception):
    pass


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


@dataclass
class BjobsQuery(ABC):
    """Base class for bjobs queries.

    If `lock` is populated, the worker task will take the lock and set
    `processing` to True before doing anything else with the query. This
    is intended to make it possible to safely update a query whilst it's
    in the queue.

    NB: this means that if you hold `lock` for an extended period,
    processing of bjobs queries will stop until you release it!
    """
    fut: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future(), init=False)
    lock: asyncio.Lock | None = field(default_factory=asyncio.Lock, init=False)
    processing: bool = field(default=False, init=False)

    @abstractmethod
    def to_args(self) -> list[str]:
        """Get a list of arguments to pass to bjobs."""
        raise NotImplementedError


@dataclass
class BjobsQueryUser(BjobsQuery):
    """Request bjobs output for a user."""
    user: str
    lock: None = field(default=None, init=False)

    def to_args(self) -> list[str]:
        assert self.user != "all", "bjobs treats the 'all' user specially"
        return ["-u", self.user]


@dataclass
class BjobsQueryIds(BjobsQuery):
    """Request bjobs output for a list of job IDs."""
    ids: set[str]

    def to_args(self) -> list[str]:
        # LSF job IDs can be up to 10 digits long, so n jobs will take
        # at most 11n - 1 bytes of argument space. If we allow ourselves
        # 2001 bytes for job IDs, we arrive at a maximum of 182 job IDs
        # per invocation.
        #
        # (POSIX specifies that at least 4096 bytes are available for
        # arguments plus environment, but this is smaller than the
        # environment variables alone on my system, so we can't usefully
        # rely on that...)
        assert 0 < len(self.ids) < 182
        return list(self.ids)


class FarmerReporter:
    queue_update_handler: Callable[..., Awaitable] | None = None
    _bjobs_worker_instance: asyncio.Task | None = None

    def __init__(self) -> None:
        self._cluster_name: str | asyncio.Future[str] | None = None
        # permit at most this many simultaneous bjobs invocations
        self._bjobs_sem = asyncio.BoundedSemaphore(1)
        self._bjobs_queue: asyncio.Queue[BjobsQuery] = asyncio.Queue()
        self._last_bjobs_call = time.monotonic_ns()
        self._latest_bjobs_ids_query: BjobsQueryIds | None = None

    async def start(self):
        self._bjobs_worker_instance = asyncio.create_task(self._bjobs_worker())

    async def stop(self):
        self._bjobs_worker_instance.cancel()
        await asyncio.gather(self._bjobs_worker_instance, return_exceptions=True)

    async def get_cluster_name(self) -> str:
        """Gets the name of the current LSF cluster."""
        if isinstance(self._cluster_name, str):
            # fast path
            return self._cluster_name
        if asyncio.isfuture(self._cluster_name):
            # another task is already doing the work
            return await self._cluster_name
        self._cluster_name = fut = asyncio.get_running_loop().create_future()
        try:
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
                    fut.set_result(self._cluster_name)
                    return self._cluster_name
            assert False, f"lsid failed: {stdout}"
        except Exception as e:
            # prevent any further lsid calls for a bit, in case this is
            # a transient problem that we could make worse by hammering
            await asyncio.sleep(LSID_BACKOFF)
            # try again next call
            self._cluster_name = None
            # fail any waiting tasks
            fut.set_exception(e)
            # fail ourselves
            raise

    async def _bjobs(self, *args: str):
        """Run bjobs, returning the parsed JSON result.

        This function does not ratelimit bjobs invocations, so it should
        usually not be called directly. (Otherwise, we could run bjobs
        so frequently that LSF is unable to service other requests.)
        """
        logging.info("running bjobs %r", args)
        async with self._bjobs_sem:
            # copy the environment because we need the LSF variables to get bjobs info
            bjobs_env = os.environ.copy()
            bjobs_env["LSB_DISPLAY_YEAR"] = "Y"
            try:
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
            finally:
                self._last_bjobs_call = time.monotonic_ns()
        if proc.returncode:
            raise BjobsError(f"bjobs exited with {proc.returncode}")
        try:
            jobs = json.loads(stdout)
        except json.JSONDecodeError as e:
            extract = stdout[max(e.pos - 30, 0) : e.pos + 30]
            raise BjobsError(f"bjobs produced invalid JSON at index {e.pos}: {extract!r}") from e
        return jobs

    async def _bjobs_worker(self):
        """Loop forever, handling bjobs requests.

        This worker is responsible for ratelimiting bjobs invocations,
        so there should usually only be a single worker running.
        """
        while True:
            query = await self._bjobs_queue.get()
            logging.info("got query %r", query)
            lock = query.lock or contextlib.nullcontext()
            try:
                now = time.monotonic_ns()
                waited = (now - self._last_bjobs_call) / 1e9
                assert waited >= 0
                if waited < BJOBS_MIN_INTERVAL:
                    await asyncio.sleep(BJOBS_MIN_INTERVAL - waited)
                async with lock:
                    query.processing = True
                result = await self._bjobs(*query.to_args())
            except CancelledError:
                query.fut.cancel()
                raise
            except Exception as e:
                query.fut.set_exception(e)
                await asyncio.sleep(BJOBS_FAILURE_BACKOFF)
            else:
                query.fut.set_result(result)
            finally:
                self._bjobs_queue.task_done()
                if self.queue_update_handler:
                    await self.queue_update_handler(self._bjobs_queue.qsize())

    async def bjobs(self, query: BjobsQuery) -> asyncio.Future:
        """Enqueue a bjobs command.

        The returned Future will resolve with the result of bjobs, or
        with an exception or cancellation as appropriate.
        """
        await self._bjobs_queue.put(query)
        return query.fut

    async def bjobs_for_user(self, user: str):
        """Get bjobs output for a user's currently-running jobs."""
        assert user != "all", "bjobs treats the 'all' user specially"
        fut = await self.bjobs(BjobsQueryUser(user))
        return await fut

    def _unbatch_bjobs_by_id(self, job_id: str, data):
        """Undo the batching of bjobs_by_id results."""
        data = data.copy()
        # TODO: avoid O(n^2) behaviour from every task doing its own
        #   scan over all records
        data["RECORDS"] = [r for r in data["RECORDS"] if r["JOBID"] == job_id]
        data["JOBS"] = len(data["RECORDS"])
        return data

    async def bjobs_by_id(self, job_id: str):
        """Get bjobs output for a job ID."""
        while True:
            query: BjobsQueryIds | None = self._latest_bjobs_ids_query
            if not query:
                # start a new query
                logging.debug("starting a new query for %r", job_id)
                self._latest_bjobs_ids_query = BjobsQueryIds({job_id})
                fut = await self.bjobs(self._latest_bjobs_ids_query)
                return self._unbatch_bjobs_by_id(job_id, await fut)
            # we have a pending query we might be able to add to
            async with query.lock:
                if query != self._latest_bjobs_ids_query:
                    # we were preempted! try again.
                    logging.debug("preempted (was %r now %r) when trying to add %r", query, self._latest_bjobs_ids_query, job_id)
                    continue
                if query.processing:
                    # this query is no longer mutable. try again.
                    logging.debug("too late to add %r to %r", job_id, query)
                    self._latest_bjobs_ids_query = None
                    continue
                if len(query.ids) > 150:
                    # this query is full. try again.
                    logging.debug("query %r full when trying to add %r", query, job_id)
                    self._latest_bjobs_ids_query = None
                    continue
                logging.debug("successfully adding %r to query %r", job_id, query)
                query.ids.add(job_id)
            return self._unbatch_bjobs_by_id(job_id, await query.fut)


# NB: RPC calls may only use keyword arguments, so make the methods keyword-only for clarity
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
    scheme, *rest = urllib.parse.urlparse(os.environ["FARMER_SERVER_BASE_URL"])
    match scheme:
        case "http":
            scheme = "ws"
        case "https":
            scheme = "wss"
    if cafile := os.environ.get("FARMER_SSL_CA_FILE"):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
        logging.info("TLS: %r", cafile)
    else:
        ssl_context = None
    async with WebSocketRpcClient(
            urllib.parse.urljoin(urlunparse((scheme, *rest)), "/internal/ws"),
            FarmerReporterRpc(reporter),
            on_disconnect=[on_disconnect],
            ssl=ssl_context,
    ) as client:
        reporter.queue_update_handler = client.other.update_queue_length
        await disconnected.wait()
    await reporter.stop()
    asyncio.get_running_loop().stop()


def main():
    aiorun.run(async_main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
