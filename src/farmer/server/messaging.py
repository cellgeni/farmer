from farmer.server import slack_bot, slack_app_client


def format_efficiency(job: dict) -> str:
    """Format job CPU/memory efficiency stats.

    Example:
        Efficiency: 2.30% of 4 CPUs, 4.50% of 4 G mem
    """
    memlimit = job["MEMLIMIT"]
    return "Efficiency: " + "".join([
        f"{job['AVERAGE_CPU_EFFICIENCY']} of {job['NALLOC_SLOT']} CPUs",
        f", {job['MEM_EFFICIENCY']} of {memlimit} mem" if memlimit else " (no memlimit set)",
    ])


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
