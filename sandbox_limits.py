"""Set limits in a fresh process before replacing it with the grader command."""
import os
import resource
import sys


def main():
    memory = int(sys.argv[1])
    if sys.platform.startswith("linux"):
        for limit, maximum in ((resource.RLIMIT_AS, memory), (resource.RLIMIT_NPROC, 256)):
            _, inherited = resource.getrlimit(limit)
            target = maximum if inherited == resource.RLIM_INFINITY else min(maximum, inherited)
            resource.setrlimit(limit, (target, target))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    file_limit = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
