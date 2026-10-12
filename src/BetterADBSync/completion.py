"""Shell completion support for Bash, Zsh and Fish.

`adbsync completion bash|zsh|fish` prints a completion script to stdout. The
scripts are generated at runtime from the argparse parser (the single source of
truth for subcommands and options) plus the per-shell templates held in this
module, so no loose data files need to be shipped with the package. Checked-in
snapshots of the generated scripts live in ``completions/`` for review and for
the test suite (``tests/test_completion.py`` asserts they are up to date).

The generated scripts complete:

- the subcommands (push, pull, completion),
- all long/short options and flags derived from the argparse parser,
- local path arguments with each shell's native file completion,
- remote (Android) path arguments by listing the *immediate children* of the
  remote parent directory (never recursive) via the hidden helper subcommand
  ``adbsync completion-remote-paths DIR``.

Remote path helper output convention: one child per line; directories are
suffixed with a trailing ``/``, plain files have no suffix. The helper
enforces a short internal timeout (see REMOTE_PATHS_TIMEOUT_SECONDS) so it
can never hang a shell, and on any error -- no device, no adb binary, timeout,
unparsable output -- it prints nothing and exits 0. The generated shell
scripts swallow its stderr and treat empty output as "no completions".

Completion is strictly opt-in: nothing here ever modifies a user's shell
startup files.
"""

import argparse
import shutil
import stat
import sys
import threading
from typing import List

from . import adb
from .argparsing import Args, build_parser
from .FileSystems.Android import AndroidFileSystem

SHELLS = ("bash", "zsh", "fish")

# The remote-path helper must stay well under the ~1.5s budget so tab
# completion stays responsive. macOS ships no `timeout` binary, so the
# timeout is enforced in Python (see _list_remote_children).
REMOTE_PATHS_TIMEOUT_SECONDS = 1.25


# ---------------------------------------------------------------------------
# Parser introspection: derive everything from the argparse definition so the
# completion scripts never duplicate a hand-maintained flag list.
# ---------------------------------------------------------------------------

def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("adbsync parser has no subparsers action")


def _visible_options(parser: argparse.ArgumentParser) -> List[argparse.Action]:
    """All top-level options (long and short) that are not hidden."""
    return [
        action for action in parser._actions
        if action.option_strings and action.help != argparse.SUPPRESS
    ]


def _visible_subcommands(parser: argparse.ArgumentParser) -> List[tuple]:
    """Visible subcommands as (name, help) tuples; hidden helpers are skipped."""
    subparsers_action = _subparsers_action(parser)
    return [
        (choice_action.metavar, choice_action.help)
        for choice_action in subparsers_action._choices_actions
        if choice_action.help != argparse.SUPPRESS
    ]


def _completion_shell_choices(parser: argparse.ArgumentParser) -> List[str]:
    """The valid arguments of the `completion` subcommand, from the parser."""
    subparsers_action = _subparsers_action(parser)
    completion_parser = subparsers_action.choices["completion"]
    for action in completion_parser._actions:
        if getattr(action, "dest", None) == "direction_completion_shell" and action.choices:
            return list(action.choices)
    return list(SHELLS)


def _sanitize_description(text: str) -> str:
    """Flatten help text for embedding in a completion script."""
    text = " ".join(str(text).split())
    # Characters with special meaning inside shell completion specifications.
    text = text.replace("'", "")
    for char in ("[", "]", '"', "`", "\\", "\n", "\r"):
        text = text.replace(char, " ")
    return " ".join(text.split())


def _zsh_description(text: str) -> str:
    """Flatten help text for a zsh _arguments spec (single-quoted, no colons)."""
    return _sanitize_description(text).replace(":", " -")


def _fish_quote(text: str) -> str:
    """Escape a string for a single-quoted fish string argument."""
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


def _option_takes_value(action: argparse.Action) -> bool:
    return action.nargs != 0


def _zsh_argument_fields(action: argparse.Action) -> str:
    """`_arguments` value fields for an option that consumes arguments."""
    if isinstance(action.nargs, int) and action.nargs >= 2:
        if isinstance(action.metavar, tuple):
            metas = action.metavar
        else:
            metas = tuple(f"ARG{i + 1}" for i in range(action.nargs))
        # space action = do not attempt any completion for these values
        return "".join(f":{meta}: " for meta in metas)
    meta = action.metavar if isinstance(action.metavar, str) else action.dest.upper()
    if action.dest in ("exclude_from", "adb_bin"):
        return f":{meta}:_files"
    return f":{meta}: "


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

_BASH_TEMPLATE = '''\
# Bash completion for adbsync.
# Generated by `adbsync completion bash` -- do not edit by hand;
# regenerate it after changing the CLI.

# List the immediate children of a remote (Android) directory (directories are
# suffixed with "/" by the helper). Helper errors and its internal timeout are
# swallowed: an empty result simply means "no completions".
_adbsync_remote_paths() {
    local cur="$1"
    shift
    local parent prefix
    parent="${{cur%/*}}"
    if [[ -z "$parent" ]]; then
        parent="/"
        prefix="/"
    else
        prefix="${{parent}}/"
    fi
    local child full
    while IFS= read -r child; do
        [[ -n "$child" ]] || continue
        full="${{prefix}}${{child}}"
        if [[ "$full" == "$cur"* ]]; then
            COMPREPLY+=("$full")
        fi
    done < <(adbsync "$@" completion-remote-paths "$parent" 2>/dev/null)
}

_adbsync() {{
    local cur prev
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    # Scan the words typed so far: find the subcommand, count its positional
    # arguments and collect adb options to forward to the remote helper.
    local sub="" word
    local npos=0
    local -a adb_args=()
    local -i i=1
    local -i skip=0
    for ((; i < COMP_CWORD; i++)); do
        word="${{COMP_WORDS[i]}}"
        if (( skip > 0 )); then
            skip=$(( skip - 1 ))
            continue
        fi
        case "$word" in
            --adb-bin|--adb-flag)
                if (( i + 1 < COMP_CWORD )); then
                    adb_args+=("$word" "${{COMP_WORDS[i+1]}}")
                    skip=1
                fi
                continue
                ;;
            --adb-option)
                if (( i + 2 < COMP_CWORD )); then
                    adb_args+=("$word" "${{COMP_WORDS[i+1]}}" "${{COMP_WORDS[i+2]}}")
                    skip=2
                fi
                continue
                ;;
            --exclude|--exclude-from|--adb-encoding)
                skip=1
                continue
                ;;
            --*=*)
                continue
                ;;
            -*)
                continue
                ;;
        esac
        if [[ -z "$sub" ]]; then
            sub="$word"
        else
            npos=$(( npos + 1 ))
        fi
    done

    local opts="__ADBSYNC_OPTIONS__"
    local subs="__ADBSYNC_SUBCOMMANDS__"

    # Completion for options that take a value.
    case "$prev" in
        --adb-bin|--exclude-from)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        --exclude|--adb-flag|--adb-option|--adb-encoding)
            COMPREPLY=()
            return 0
            ;;
    esac

    # Before any subcommand: complete subcommand names or global options.
    if [[ -z "$sub" ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$subs" -- "$cur") )
        fi
        return 0
    fi

    # Global options are only accepted before the subcommand (argparse
    # semantics), so offer none after it.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=()
        return 0
    fi

    case "$sub" in
        push|pull)
            local -i remote_pos=1
            if [[ "$sub" == "pull" ]]; then
                remote_pos=0
            fi
            if (( npos == remote_pos )); then
                _adbsync_remote_paths "$cur" "${{adb_args[@]}}"
            else
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            return 0
            ;;
        completion)
            COMPREPLY=( $(compgen -W "__ADBSYNC_SHELLS__" -- "$cur") )
            return 0
            ;;
    esac

    COMPREPLY=()
    return 0
}}

complete -F _adbsync adbsync
'''

_ZSH_TEMPLATE = '''\
#compdef adbsync
# Zsh completion for adbsync.
# Generated by `adbsync completion zsh` -- do not edit by hand;
# regenerate it after changing the CLI.

# List the immediate children of a remote (Android) directory (directories are
# suffixed with "/" by the helper). Helper errors and its internal timeout are
# swallowed: an empty result simply means "no completions". Relies on the
# dynamically scoped $adb_args set up by _adbsync below.
_adbsync_remote() {{
    local parent prefix
    if [[ "$PREFIX" != */* ]]; then
        parent=""
        prefix=""
    else
        parent="${{PREFIX:h}}"
        if [[ "$parent" == "/" ]]; then
            prefix="/"
        else
            prefix="${{parent}}/"
        fi
    fi
    local -a entries candidates
    entries=("${{(@f)$(adbsync ${{adb_args[@]+"${{adb_args[@]}}"}} completion-remote-paths "${{parent:-/}}" 2>/dev/null)}}")
    local entry
    for entry in "${{entries[@]}}"; do
        [[ -n "$entry" ]] || continue
        candidates+=("${{prefix}}${{entry}}")
    done
    compadd -Q -a candidates
}}

_adbsync() {{
    # Collect the adb options typed so far so remote path queries honor
    # --adb-bin/--adb-flag/--adb-option.
    local w
    local -a adb_args
    local -i i=2
    while (( i < CURRENT )); do
        w="${{words[i]}}"
        case "$w" in
            --adb-bin|--adb-flag)
                if (( i + 1 < CURRENT )); then
                    adb_args+=("$w" "${{words[i+1]}}")
                    i=$(( i + 1 ))
                fi
                ;;
            --adb-option)
                if (( i + 2 < CURRENT )); then
                    adb_args+=("$w" "${{words[i+1]}}" "${{words[i+2]}}")
                    i=$(( i + 2 ))
                fi
                ;;
        esac
        i=$(( i + 1 ))
    done

    local context state state_descr line
    typeset -A opt_args

    _arguments -C \\
__ADBSYNC_ZSH_OPTION_SPECS__ \\
        '1: :->subcommand' \\
        '*:: :->args' \\
        && return 0

    case $state in
        subcommand)
            local -a subcommands
            subcommands=(__ADBSYNC_SUBCOMMANDS_ARRAY__)
            _describe -t subcommands 'adbsync command' subcommands
            ;;
        args)
            case "${{words[1]}}" in
                push)
                    _arguments \\
                        '1:local path:_files' \\
                        '2:android path:_adbsync_remote'
                    ;;
                pull)
                    _arguments \\
                        '1:android path:_adbsync_remote' \\
                        '2:local path:_files'
                    ;;
                completion)
                    _arguments \\
                        '1:shell:(__ADBSYNC_SHELLS_ARRAY__)'
                    ;;
            esac
            ;;
    esac
}}

_adbsync "$@"
'''

_FISH_TEMPLATE = '''\
# Fish completion for adbsync.
# Generated by `adbsync completion fish` -- do not edit by hand;
# regenerate it after changing the CLI.

# Parse the current command line; prints "SUBCOMMAND;POSITIONALS_COMPLETED" on
# the first line followed by any adb options typed so far, one per line.
function __adbsync_parse
    set -l tokens (commandline -opc)
    set -e tokens[1] # drop the command name itself
    set -l sub ''
    set -l npos 0
    set -l adb_args
    set -l skip 0
    set -l i 1
    set -l n (count $tokens)
    while test $i -le $n
        set -l token $tokens[$i]
        if test $skip -gt 0
            set skip (math $skip - 1)
        else
            switch $token
                case '--adb-bin' '--adb-flag'
                    if test $i -lt $n
                        set -a adb_args $token $tokens[(math $i + 1)]
                        set skip 1
                    end
                case '--adb-option'
                    if test (math $i + 1) -le $n
                        set -a adb_args $token $tokens[(math $i + 1)] $tokens[(math $i + 2)]
                        set skip 2
                    end
                case '--exclude' '--exclude-from' '--adb-encoding'
                    set skip 1
                case '--*' '-*' # valueless flags, incl. -h/--help
                case '*'
                    if test -z "$sub"
                        set sub $token
                    else
                        set npos (math $npos + 1)
                    end
            end
        end
        set i (math $i + 1)
    end
    printf '%s;%s\\n' "$sub" "$npos"
    for adb_arg in $adb_args
        printf '%s\\n' "$adb_arg"
    end
end

# Refresh the global scratch variables _adbsync_sub, _adbsync_npos and
# _adbsync_adb_args for the current command line.
function __adbsync_state
    set -l lines (__adbsync_parse)
    set -l info (string split -m 1 ';' -- $lines[1])
    set -g _adbsync_sub $info[1]
    set -g _adbsync_npos $info[2]
    set -g _adbsync_adb_args
    if test (count $lines) -gt 1
        set -g _adbsync_adb_args $lines[2..-1]
    end
end

# True while completing a local path argument (push's 1st, pull's 2nd).
function __adbsync_is_local
    __adbsync_state
    if test "$_adbsync_sub" = push
        test "$_adbsync_npos" = 0
    else if test "$_adbsync_sub" = pull
        test "$_adbsync_npos" = 1
    else
        return 1
    end
end

# True while completing a remote (Android) path argument (push's 2nd, pull's
# 1st).
function __adbsync_is_remote
    __adbsync_state
    if test "$_adbsync_sub" = push
        test "$_adbsync_npos" = 1
    else if test "$_adbsync_sub" = pull
        test "$_adbsync_npos" = 0
    else
        return 1
    end
end

# True while completing the SHELL argument of the completion subcommand.
function __adbsync_is_completion_shell
    __adbsync_state
    test "$_adbsync_sub" = completion
    and test "$_adbsync_npos" = 0
end

# List the immediate children of a remote (Android) directory (directories are
# suffixed with "/" by the helper). Helper errors and its internal timeout are
# swallowed: an empty result simply means "no completions".
function __adbsync_remote_paths
    __adbsync_state
    set -l cur (commandline -ct)
    set -l parent
    if string match -q '*/*' -- $cur
        set parent (string replace -r '/[^/]*$' '' -- $cur)
    end
    if test -z "$parent"
        set parent /
        set prefix /
    else if test "$parent" = /
        set prefix /
    else
        set prefix $parent/
    end
    for entry in (adbsync $_adbsync_adb_args completion-remote-paths $parent 2>/dev/null)
        printf '%s\\n' "$prefix$entry"
    end
end

# Disable file completion by default; it is re-enabled for local arguments.
complete -c adbsync -f

# Subcommands
__ADBSYNC_FISH_SUBCOMMAND_COMPLETIONS__

# Global options (accepted only before the subcommand, like argparse)
__ADBSYNC_FISH_OPTION_COMPLETIONS__

# Positional arguments: local paths get native file completion, remote paths
# are listed by the internal helper, and the completion subcommand takes the
# shell names.
complete -c adbsync -n '__adbsync_is_local' -F
complete -c adbsync -n '__adbsync_is_remote' -a '(__adbsync_remote_paths)'
complete -c adbsync -n '__adbsync_is_completion_shell' -a 'bash zsh fish'
'''


def _render_template(template: str) -> str:
    """Unescape doubled braces in a template (written doubled so shell
    `${...}`/`(...)` constructs survive; substitution uses __TOKEN__ replace)."""
    return template.replace("{{", "{").replace("}}", "}")


def _generate_bash(parser: argparse.ArgumentParser) -> str:
    options = [
        option_string
        for action in _visible_options(parser)
        for option_string in action.option_strings
    ]
    subcommands = [name for name, _help in _visible_subcommands(parser)]
    script = _render_template(_BASH_TEMPLATE)
    script = script.replace("__ADBSYNC_OPTIONS__", " ".join(options))
    script = script.replace("__ADBSYNC_SUBCOMMANDS__", " ".join(subcommands))
    script = script.replace("__ADBSYNC_SHELLS__", " ".join(_completion_shell_choices(parser)))
    return script


def _generate_zsh(parser: argparse.ArgumentParser) -> str:
    spec_lines = []
    for action in _visible_options(parser):
        description = _zsh_description(action.help or "")
        if _option_takes_value(action):
            value_fields = _zsh_argument_fields(action)
        else:
            value_fields = ""
        for option_string in action.option_strings:
            spec_lines.append(f"        '{option_string}[{description}]{value_fields}'")
    subcommands = [name for name, _help in _visible_subcommands(parser)]
    script = _render_template(_ZSH_TEMPLATE)
    # Spec lines are joined with a line continuation; the template supplies the
    # final continuation before the positional specs.
    script = script.replace("__ADBSYNC_ZSH_OPTION_SPECS__", " \\\n".join(spec_lines))
    script = script.replace("__ADBSYNC_SUBCOMMANDS_ARRAY__", " ".join(subcommands))
    script = script.replace("__ADBSYNC_SHELLS_ARRAY__", " ".join(_completion_shell_choices(parser)))
    return script


def _generate_fish(parser: argparse.ArgumentParser) -> str:
    subcommand_lines = [
        "complete -c adbsync -n '__fish_use_subcommand' -a {} -d '{}'".format(
            name, _fish_quote(_sanitize_description(help_text))
        )
        for name, help_text in _visible_subcommands(parser)
    ]
    option_lines = []
    for action in _visible_options(parser):
        specs = ["complete -c adbsync -n '__fish_use_subcommand'"]
        for option_string in action.option_strings:
            if option_string.startswith("--"):
                specs.append(f"-l {option_string[2:]}")
            else:
                specs.append(f"-s {option_string[1:]}")
        if _option_takes_value(action):
            specs.append("-r")
        if action.help:
            specs.append(f"-d '{_fish_quote(_sanitize_description(action.help))}'")
        option_lines.append(" ".join(specs))
    script = _render_template(_FISH_TEMPLATE)
    script = script.replace("__ADBSYNC_FISH_SUBCOMMAND_COMPLETIONS__", "\n".join(subcommand_lines))
    script = script.replace("__ADBSYNC_FISH_OPTION_COMPLETIONS__", "\n".join(option_lines))
    return script


_GENERATORS = {
    "bash": _generate_bash,
    "zsh": _generate_zsh,
    "fish": _generate_fish,
}


def generate_completion_script(shell: str) -> str:
    """Generate the completion script for `shell` from the argparse parser."""
    parser = build_parser("", "")
    if shell not in _GENERATORS:
        raise ValueError(f"unsupported shell {shell!r}, expected one of {', '.join(SHELLS)}")
    return _GENERATORS[shell](parser)


# ---------------------------------------------------------------------------
# Command dispatch (called from main())
# ---------------------------------------------------------------------------

def run_completion_command(args: Args) -> int:
    """Handle the `completion` subcommand and the hidden remote-path helper."""
    if args.direction == "completion":
        sys.stdout.write(generate_completion_script(args.direction_completion_shell))
        sys.stdout.flush()
        return 0
    return _completion_remote_paths_helper(args)


def _build_adb_arguments(args: Args) -> List[str]:
    """Same adb argument construction as the transfer path in main()."""
    if args.adb_bin is None:
        adb_bin = adb.find_adb_executable()
    else:
        adb_bin = args.adb_bin
    adb_arguments = [adb_bin] + [f"-{arg}" for arg in args.adb_flags]
    for option, value in args.adb_options:
        adb_arguments.append(f"-{option}")
        adb_arguments.append(value)
    return adb_arguments


def _completion_remote_paths_helper(args: Args) -> int:
    """`adbsync completion-remote-paths DIR`: print remote children, or nothing.

    Never fails: on any error (no adb, no device, unparsable output, timeout)
    it prints nothing to stdout and exits 0, so the calling shell simply gets
    no completions and can never hang.
    """
    try:
        adb_arguments = _build_adb_arguments(args)
    except Exception:
        return 0
    # Resolve the binary up front: a missing adb exits quietly instead of
    # leaving a half-constructed AndroidFileSystem behind.
    if shutil.which(adb_arguments[0]) is None:
        return 0
    children = _list_remote_children(
        adb_arguments,
        args.adb_encoding,
        args.completion_remote_paths_dir
    )
    for child in children:
        print(child)
    sys.stdout.flush()
    return 0


def _list_remote_children(
    adb_arguments: List[str],
    adb_encoding: str,
    remote_dir: str,
    timeout: float = REMOTE_PATHS_TIMEOUT_SECONDS
) -> List[str]:
    """Immediate children of `remote_dir`; directories get a "/" suffix.

    The listing runs in a daemon thread so a hung adb (e.g. a stalled server
    start) cannot block the shell beyond `timeout` seconds; on timeout no
    suggestions are returned. Any exception -- no device, permission denied,
    unparsable ls output -- yields no suggestions. The helper's adb shell
    process is killed on the way out so nothing lingers.
    """
    children: List[str] = []

    def worker() -> None:
        fs_android = None
        try:
            fs_android = AndroidFileSystem(adb_arguments, adb_encoding, True)
            for name, stat_result in fs_android.lstat_in_dir(remote_dir):
                if name is None:
                    # AndroidFileSystem does not capture symlink names; skip.
                    continue
                if name in (".", ".."):
                    # Never suggest the self/parent entries some ls
                    # implementations emit.
                    continue
                if stat.S_ISDIR(stat_result.st_mode):
                    children.append(name + "/")
                else:
                    children.append(name)
        except BaseException:
            # Degrade gracefully: no suggestions on any error.
            children.clear()
        finally:
            if fs_android is not None:
                try:
                    fs_android.proc_adb_shell.stdin.close()
                except Exception:
                    pass
                try:
                    fs_android.proc_adb_shell.kill()
                except Exception:
                    pass

    listing_thread = threading.Thread(
        target=worker,
        name="adbsync-completion-remote-paths",
        daemon=True
    )
    listing_thread.start()
    listing_thread.join(timeout)
    if listing_thread.is_alive():
        # Timed out: the daemon thread dies with the process, nothing is
        # returned. This must never hang a shell.
        return []
    return children
