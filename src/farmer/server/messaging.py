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
