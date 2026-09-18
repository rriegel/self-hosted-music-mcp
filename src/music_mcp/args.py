"""Shared CLI plumbing: global flags accepted anywhere in argv.

Argparse subparsers only recognize parent flags before the subcommand; users
naturally write them after ("scan --db x"). Both orders should work. This helper
pulls known flags out of argv and reinstates them at the front.
"""

from __future__ import annotations


def split_global_flags(
    argv: list[str], value_flags: set[str], bool_flags: set[str]
) -> tuple[dict[str, str | None], list[str]]:
    """Pull known global flags out of argv wherever they appear.

    Returns ({flag: value-or-None-for-bool}, remaining argv without them).
    Last occurrence of a repeated flag wins.
    """
    pulled: dict[str, str | None] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in value_flags and i + 1 < len(argv):
            pulled[token] = argv[i + 1]
            i += 2
        elif token in bool_flags:
            pulled[token] = None
            i += 1
        else:
            rest.append(token)
            i += 1
    return pulled, rest


def reinstate(pulled: dict[str, str | None]) -> list[str]:
    """Rebuild flag tokens so a parent argparse parser sees them again."""
    tokens: list[str] = []
    for flag, value in pulled.items():
        tokens.append(flag)
        if value is not None:
            tokens.append(value)
    return tokens
