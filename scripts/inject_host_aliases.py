#!/usr/bin/env python3
"""Inject hostAliases into rendered Job and CronJob pod templates."""

from __future__ import annotations

import argparse
import re
import sys


def _transform_doc(lines: list[str], host: str, ip: str) -> list[str]:
    kind = None
    for line in lines:
        match = re.match(r"^kind:\s*(\w+)$", line.strip())
        if match:
            kind = match.group(1)
            break

    if kind not in {"Job", "CronJob"}:
        return lines

    out: list[str] = []
    inserted = False
    for line in lines:
        if not inserted:
            match = re.match(r"^(\s+)(initContainers|containers):\s*$", line)
            if match:
                indent = match.group(1)
                out.extend(
                    [
                        f"{indent}hostAliases:",
                        f"{indent}  - ip: {ip}",
                        f"{indent}    hostnames:",
                        f"{indent}      - {host}",
                    ]
                )
                inserted = True
        out.append(line)

    if not inserted:
        for index, line in enumerate(out):
            match = re.match(r"^(\s+)restartPolicy:\s*Never\s*$", line)
            if match:
                indent = match.group(1)
                out[index:index] = [
                    f"{indent}hostAliases:",
                    f"{indent}  - ip: {ip}",
                    f"{indent}    hostnames:",
                    f"{indent}      - {host}",
                ]
                break

    return out


def inject(text: str, host: str, ip: str) -> str:
    output: list[str] = []
    buffer: list[str] = []

    for line in text.splitlines():
        if line.strip() == "---":
            output.extend(_transform_doc(buffer, host, ip))
            output.append(line)
            buffer = []
            continue
        buffer.append(line)

    output.extend(_transform_doc(buffer, host, ip))
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()

    sys.stdout.write(inject(sys.stdin.read(), args.host, args.ip))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
