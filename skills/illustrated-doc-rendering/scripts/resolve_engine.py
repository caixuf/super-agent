#!/usr/bin/env python3
"""Locate the document engine without host-specific paths or filesystem scans."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def checked_root(value):
    if not str(value).strip():
        raise ValueError('engine path must not be empty')
    root = Path(value).expanduser().resolve()
    if not (root / 'tools' / 'arch_doc.py').is_file() or not (root / 'tools' / 'doc_browser.py').is_file():
        raise ValueError('engine must contain tools/arch_doc.py and tools/doc_browser.py')
    return root


def resolve_engine(explicit=None):
    if explicit is not None:
        return checked_root(explicit)
    configured = os.environ.get('ARCH_DOC_TOOL')
    if configured:
        tool = Path(configured).expanduser().resolve()
        if tool.name != 'arch_doc.py' or tool.parent.name != 'tools' or not tool.is_file():
            raise ValueError('ARCH_DOC_TOOL must name an existing tools/arch_doc.py file')
        return checked_root(tool.parent.parent)
    # Source: <repo>/skills/<skill>/scripts/resolve_engine.py.
    # Installed: <profile>/skills/<skill>/scripts/resolve_engine.py, with the
    # managed engine at <profile>/agentcore. Only these bounded paths are tried.
    for script in (Path(__file__).absolute(), Path(__file__).resolve()):
        try:
            ancestor = script.parents[3]
        except IndexError:
            continue
        for candidate in (ancestor, ancestor / 'agentcore'):
            if (candidate / 'tools' / 'arch_doc.py').is_file() and (candidate / 'tools' / 'doc_browser.py').is_file():
                return checked_root(candidate)
    raise ValueError('document engine unavailable; pass --engine-root or set ARCH_DOC_TOOL')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root')
    args = parser.parse_args()
    try:
        print(resolve_engine(args.engine_root))
    except (OSError, ValueError) as error:
        parser.exit(2, 'ERROR: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
