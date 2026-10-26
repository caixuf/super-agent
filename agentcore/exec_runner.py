"""POSIX runner bootstrap: apply output file limit before executing trusted argv.

Executed in a fresh interpreter; no preexec_fn inside a multi-threaded worker.
This bounds individual files, not total disk or process resources; not a sandbox.
"""
import os
import resource
import sys


def main():
    limit = int(sys.argv[1])
    if limit <= 0 or len(sys.argv) < 3:
        raise ValueError('positive file limit and command required')
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == '__main__':
    main()
