#!/usr/bin/python3

"""Hancho is a simple, pleasant build system."""

import argparse
import asyncio
import builtins
import inspect
import io
import json
import os
import re
import subprocess
import sys
import traceback
from turtle import st
import types
from pathlib import Path
from glob import glob

# If we were launched directly, a reference to this module is already in
# sys.modules[__name__]. Stash another reference in sys.modules["hancho"] so
# that build.hancho and descendants don't try to load a second copy of Hancho.

this = sys.modules[__name__]
sys.modules["hancho"] = sys.modules[__name__]

################################################################################
# Build rule helper methods


def abspath(path):
    """
    Pathlib's path.absolute() doesn't resolve "foo/../bar", so we use
    os.path.abspath.
    """
    return Path(os.path.abspath(path))


def relpath(path1, path2):
    """
    Pathlib's path.relative_to() refuses to generate "../bar", so we use
    os.path.relpath.
    """
    return Path(os.path.relpath(path1, path2))


def color(red=None, green=None, blue=None):
    """
    Converts RGB color to ANSI format string
    """
    # Color strings don't work in Windows console, so don't emit them.
    if os.name == "nt":
        return ""
    if red is None:
        return "\x1B[0m"
    return f"\x1B[38;2;{red};{green};{blue}m"


def run_cmd(cmd):
    """
    Runs a console command and returns its stdout with whitespace stripped
    """
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def swap_ext(name, new_ext):
    """
    Replaces file extensions on either a single filename or a list of filenames
    """
    if name is None:
        return None
    if isinstance(name, list):
        return [swap_ext(n, new_ext) for n in name]
    else:
        return Path(name).with_suffix(new_ext)


def mtime(filename):
    """Gets the file's mtime and tracks how many times we called it"""
    this.mtime_calls += 1
    return Path(filename).stat().st_mtime


def maybe_as_number(text):
    """
    Tries to convert a string to an int, then a float, then gives up. Used for
    ingesting unrecognized flag values.
    """
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


################################################################################


def flatten(elements):
    """
    Converts an arbitrarily-nested list 'elements' into a flat list, or wraps it
    in [] if it's not a list.
    """
    if isinstance(elements, Task):
        assert not inspect.isawaitable(elements.promise)
        assert isinstance(elements.promise, list)
        return flatten(elements.promise)

    if not isinstance(elements, list):
        return [elements]

    result = []
    for element in elements:
        result.extend(flatten(element))
    return result


################################################################################

template_regex = re.compile("{[^}]*}")


def expand_async(rule, template):
    """
    A trivial templating system that replaces {foo} with the value of rule.foo
    and keeps going until it can't replace anything. Templates that evaluate to
    None are replaced with the empty string.
    """

    # Cancellations cancel the current task
    if isinstance(template, Cancel):
        raise template

    if isinstance(template, list):
        return [expand_async(rule, x) for x in template]

    if isinstance(template, Path):
        return Path(expand_async(rule, str(template)))

    if inspect.isfunction(template):
        return template

    if template is None:
        return ""

    if not isinstance(template, str):
        print(f"Bad type {type(template)}")
        sys.exit(-1)

    for _ in range(20):
        if not template_regex.search(template):
            return template

        result = ""
        while span := template_regex.search(template):
            result += template[0 : span.start()]
            exp = template[span.start() : span.end()]

            # Evaluate the template contents. If it fails to evaluate, pass the
            # expression directly to the output for debugging.
            replacement = ""
            try:
                replacement = eval(exp[1:-1], globals(), rule)  # pylint: disable=eval-used
            except Exception:  # pylint: disable=broad-except
                replacement = exp

            replacement = flatten(replacement)

            if not isinstance(replacement, list):
                replacement = [replacement]

            # Strip out Nones and join the replacement into one string.
            replacement = [str(r) for r in replacement if r is not None]
            result += " ".join(replacement)
            template = template[span.end() :]

        result += template
        template = result

    raise ValueError(f"Expanding '{str(template)[0:20]}...' failed to terminate")


################################################################################


async def await_variant(variant):
    """
    Recursively awaits everything awaitable in the given variant. Replaces
    awaitables in arrays & dicts with the result of the await.
    """

    # Do _not_ await the task that is currently running.
    if variant == asyncio.current_task():
        return variant

    if inspect.isawaitable(variant):
        return await await_variant(await variant)

    if isinstance(variant, Task):
        return await variant.promise

    if isinstance(variant, list):
        for i, v in enumerate(variant):
            variant[i] = await await_variant(v)
        return variant

    if isinstance(variant, dict):
        # Python complains about changing the size of a dict if we use
        # enumerate() here :P
        for key in list(variant.keys()):
            variant[key] = await await_variant(variant[key])
        return variant

    return variant



################################################################################


class Chdir:
    """
    Copied from Python 3.11 contextlib.py
    """

    def __init__(self, path):
        self.path = path
        self._old_cwd = []

    def __enter__(self):
        self._old_cwd.append(os.getcwd())
        os.chdir(self.path)

    def __exit__(self, *excinfo):
        os.chdir(self._old_cwd.pop())


################################################################################


class Config(dict):
    """
    Config is a 'bag of fields' that behaves sort of like a Javascript object.
    """

    def __init__(self, base=None, **kwargs):
        self.base = base
        self |= kwargs

    def __missing__(self, key):
        return None if self.base is None else self.base[key]

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __getattr__(self, key):
        return self.__getitem__(key)

    def __repr__(self):
        """
        Turns this config blob into a JSON doc for debugging
        """

        class Encoder(json.JSONEncoder):
            """
            Types the encoder doesn't understand just get stringified.
            """

            def default(self, o):
                return str(o)

        return json.dumps(self, indent=2, cls=Encoder)

    def extend(self, **kwargs):
        """
        Returns a 'subclass' of this config blob that can override its fields.
        """
        return type(self)(base=self, **kwargs)


################################################################################

# fmt: off
config = Config(
    filename  = "build.hancho",

    desc      = "{files_in} -> {files_out}",
    chdir     = ".",
    jobs      = os.cpu_count(),
    semaphore = None,
    verbose   = False,
    quiet     = False,
    dryrun    = False,
    debug     = False,
    force     = False,
    depformat = "gcc",

    root_dir  = Path.cwd(),
    task_dir  = "{root_dir}",
    in_dir    = "{root_dir / load_dir}",
    deps_dir  = "{root_dir / load_dir}",
    out_dir   = "{root_dir / build_dir / load_dir}",
    build_dir = Path("build"),

    files_out = [],
    deps      = [],

    len       = len,
    run_cmd   = run_cmd,
    swap_ext  = swap_ext,
    color     = color,
    glob      = glob,
    abspath   = abspath,
    relpath   = relpath,
)
# fmt: on

################################################################################

line_dirty = False  # pylint: disable=invalid-name


def log(message, *args, sameline=False, **kwargs):
    """
    Simple logger that can do same-line log messages like Ninja
    """
    if config.quiet:
        return

    if not sys.stdout.isatty():
        sameline = False

    output = io.StringIO()
    if sameline:
        kwargs["end"] = ""
    print(message, *args, file=output, **kwargs)
    output = output.getvalue()

    global line_dirty  # pylint: disable=global-statement
    if not sameline and line_dirty:
        sys.stdout.write("\n")
        line_dirty = False

    if not output:
        return

    if sameline:
        sys.stdout.write("\r")
        output = output[: os.get_terminal_size().columns - 1]
        sys.stdout.write(output)
        sys.stdout.write("\x1B[K")
    else:
        sys.stdout.write(output)

    sys.stdout.flush()
    line_dirty = output[-1] != "\n"


################################################################################


def main():
    """
    Our main() just handles command line args and delegates to async_main()
    """

    # pylint: disable=line-too-long
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("filename",        default="build.hancho", type=str, nargs="?", help="The name of the .hancho file to build")
    parser.add_argument("-C", "--chdir",   default=".",            type=str,            help="Change directory before starting the build")
    parser.add_argument("-j", "--jobs",    default=os.cpu_count(), type=int,            help="Run N jobs in parallel (default = cpu_count)")
    parser.add_argument("-v", "--verbose", default=False,          action="store_true", help="Print verbose build info")
    parser.add_argument("-q", "--quiet",   default=False,          action="store_true", help="Mute all output")
    parser.add_argument("-n", "--dryrun",  default=False,          action="store_true", help="Do not run commands")
    parser.add_argument("-d", "--debug",   default=False,          action="store_true", help="Print debugging information")
    parser.add_argument("-f", "--force",   default=False,          action="store_true", help="Force rebuild of everything")
    # fmt: on

    # Parse the command line
    (flags, unrecognized) = parser.parse_known_args()

    # Merge all known command line flags into our global config object.
    global config  # pylint: disable=global-statement
    config |= flags.__dict__

    # Unrecognized command line parameters also become global config fields if
    # they are flag-like
    for span in unrecognized:
        if match := re.match(r"-+([^=\s]+)(?:=(\S+))?", span):
            config[match.group(1)] = (
                maybe_as_number(match.group(2)) if match.group(2) is not None else True
            )

    # Change directory if needed and kick off the build.
    with Chdir(config.chdir):
        result = asyncio.run(async_main())

    return result


################################################################################


async def async_main():
    """
    All the actual Hancho stuff runs in an async context.
    """

    # Reset all global state
    this.hancho_mods = {}
    this.mod_stack = []
    this.hancho_outs = set()
    this.tasks_total = 0
    this.tasks_pass = 0
    this.tasks_fail = 0
    this.tasks_skip = 0
    this.task_counter = 0
    this.mtime_calls = 0

    # Configure our job semaphore
    # pylint: disable=attribute-defined-outside-init
    config.semaphore = asyncio.Semaphore(config.jobs)

    # Load the root build.hancho file.
    root_filename = abspath(config.filename)
    if not root_filename.exists():
        raise FileNotFoundError(f"Could not find {root_filename}")
    load_abs(root_filename)

    # Root module(s) loaded. Run all tasks in the queue until we run out.
    while True:
        pending_tasks = asyncio.all_tasks() - {asyncio.current_task()}
        if not pending_tasks:
            break
        await asyncio.wait(pending_tasks)

    # Print a copy of the global config after all tasks are done if we're in
    # debug mode
    if config.debug:
        log(f"Hancho global config: {config}")
        log("")

    # Done, print status info if needed
    if config.debug or config.verbose:
        log(f"tasks total:   {this.tasks_total}")
        log(f"tasks passed:  {this.tasks_pass}")
        log(f"tasks failed:  {this.tasks_fail}")
        log(f"tasks skipped: {this.tasks_skip}")
        log(f"mtime calls:   {this.mtime_calls}")

    if this.tasks_fail:
        log(f"hancho: {color(255, 0, 0)}BUILD FAILED{color()}")
    elif this.tasks_pass:
        log(f"hancho: {color(0, 255, 0)}BUILD PASSED{color()}")
    else:
        log(f"hancho: {color(255, 255, 0)}BUILD CLEAN{color()}")

    return -1 if this.tasks_fail else 0


################################################################################
# The .hancho file loader does a small amount of work to keep track of the
# stack of .hancho files that have been loaded.


def load(mod_path):
    """
    Searches the loaded Hancho module stack for a module whose directory
    contains 'mod_path', then loads the module relative to that path.
    """
    mod_path = Path(mod_path)
    for parent_mod in reversed(this.mod_stack):
        abs_path = abspath(Path(parent_mod.__file__).parent / mod_path)
        if abs_path.exists():
            return load_abs(abs_path)
    raise FileNotFoundError(f"Could not load module {mod_path}")


def load_abs(abs_path):
    """
    Loads a Hancho module ***while chdir'd into its directory***
    """
    abs_path = Path(abs_path)
    if abs_path in this.hancho_mods:
        return this.hancho_mods[abs_path]

    with open(abs_path, encoding="utf-8") as file:
        source = file.read()
        code = compile(source, abs_path, "exec", dont_inherit=True)

    module = type(sys)(abs_path.stem)
    module.__file__ = abs_path
    module.__builtins__ = builtins
    this.hancho_mods[abs_path] = module

    # The directory the module is in gets added to the global path so we can
    # import .py modules in the same directory as it if needed. This may not
    # be necessary.
    sys.path.insert(0, str(abs_path.parent))

    this.mod_stack.append(module)

    # We must chdir()s into the .hancho file directory before running it so that
    # glob() can resolve files relative to the .hancho file itself.
    with Chdir(abs_path.parent):
        # Why Pylint thinks this is not callable is a mystery.
        # pylint: disable=not-callable
        types.FunctionType(code, module.__dict__)()

    this.mod_stack.pop()

    return module


################################################################################


class Cancel(BaseException):
    """
    Stub exception class that's used to cancel tasks that depend on a task that
    threw a real exception.
    """


################################################################################


class Rule(Config):
    """
    Rules are callable Configs that create a Task when called.
    Rules also delegate attribute lookups to the global 'config' object if they
    are missing a field.
    """

    # pylint: disable=access-member-before-definition
    # pylint: disable=attribute-defined-outside-init
    # pylint: disable=too-many-instance-attributes

    def __init__(self, base=None, **kwargs):
        super().__init__(base, **kwargs)
        if self.rule_dir is None:
            self.rule_dir = relpath(
                Path(inspect.stack(context=0)[1].filename).parent, self.root_dir
            )

    def __missing__(self, key):
        """
        Rules delegate to config[key] if a key is missing.
        """
        result = super().__missing__(key)
        return result if result else config[key]

    def __call__(self, files_in, files_out=None, **kwargs):
        task = Task(base=self, **kwargs)
        task.files_in = files_in
        if files_out is not None:
            task.files_out = files_out

        task.call_dir = relpath(
            Path(inspect.stack(context=0)[1].filename).parent, self.root_dir
        )
        task.work_dir = relpath(Path.cwd(), self.root_dir)
        task.load_dir = relpath(Path(this.mod_stack[-1].__file__).parent, self.root_dir)

        coroutine = task.run_async()
        task.promise = asyncio.create_task(coroutine)
        return task


################################################################################


class Task(Rule):
    """
    Calling a Rule creates a Task.
    """

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=attribute-defined-outside-init

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        this.tasks_total += 1

    ########################################

    async def run_async(self):
        """
        Entry point for async task stuff, handles exceptions generated during
        task execution.
        """

        try:
            return await self.task_main()

        # If any of this tasks's dependencies were cancelled, we propagate the
        # cancellation to downstream tasks.
        except Cancel as cancel:
            this.tasks_skip += 1
            return cancel

        # If this task failed, we print the error and propagate a Cancel
        # exception to downstream tasks.
        except Exception:  # pylint: disable=broad-except
            if not self.quiet:
                log(color(255, 128, 128))
                traceback.print_exception(*sys.exc_info())
                log(color())
            this.tasks_fail += 1
            return Cancel()

        finally:
            if self.debug:
                log("")

    ########################################

    async def task_main(self):
        """
        All the steps needed to run a task and check the result.
        """

        # Check for missing fields
        if not self.command:  # pylint: disable=access-member-before-definition
            raise ValueError("Task missing command")
        if self.files_in is None:
            raise ValueError("Task missing files_in")
        if self.files_out is None:
            raise ValueError("Task missing files_out")

        # Await everything
        for key in list(self.keys()):
            self[key] = await await_variant(self[key])

        # Flatten everything
        self.flatten()

        # Expand everything
        self.expand()

        # Check for missing inputs
        for file in self.abs_files_in:
            if not file.exists():
                raise NameError(f"Input file doesn't exist - {file}")

        # Check for duplicate task outputs
        for file in self.abs_files_out:
            if file in this.hancho_outs:
                raise NameError(f"Multiple rules build {file}!")
            this.hancho_outs.add(file)

        # Check if we need a rebuild
        self.reason = self.needs_rerun(self.force)
        if not self.reason:
            this.tasks_skip += 1
            return self.abs_files_out

        # Make sure our output directories exist
        if not self.dryrun:
            for file_out in self.abs_files_out:
                file_out.parent.mkdir(parents=True, exist_ok=True)

        # Run the commands
        result = await self.run_commands()

        # Check if the commands actually updated all the output files
        if self.files_in and self.files_out and not self.dryrun:
            if second_reason := self.needs_rerun():
                raise ValueError(
                    f"Task '{self.desc}' still needs rerun after running!\n"
                    + f"Reason: {second_reason}"
                )

        return result

    ########################################

    def flatten(self):
        # Flatten all filename promises in any of the input filename arrays.
        self.files_in = flatten(self.files_in)
        self.files_out = flatten(self.files_out)
        self.deps = flatten(self.deps)
        self.command = flatten(self.command)

    ########################################

    def expand(self):
        """
        Expands all template strings in the task.
        NOTE: The order that fields are expanded _does_ matter. For example,
        if we call swap_ext() on a filename array that hasn't been awaited,
        we would end up trying to change the file extension of a promise and
        that would be bad.
        """

        # Expand all our directories
        self.in_dir = Path(expand_async(self, self.in_dir))
        self.deps_dir = Path(expand_async(self, self.deps_dir))
        self.out_dir = Path(expand_async(self, self.out_dir))
        self.task_dir = Path(expand_async(self, self.task_dir))

        self.files_in = expand_async(self, self.files_in)
        self.files_out = expand_async(self, self.files_out)
        self.deps = expand_async(self, self.deps)

        # Prepend directories to filenames and then normalize + absolute them.
        # If they're already absolute, this does nothing.
        self.abs_files_in = [abspath(self.in_dir / f) for f in self.files_in]
        self.abs_files_out = [abspath(self.out_dir / f) for f in self.files_out]
        self.abs_deps = [abspath(self.deps_dir / f) for f in self.deps]

        # Strip task_dir off the absolute paths to produce task_dir-relative
        # paths
        self.files_in = [relpath(f, self.task_dir) for f in self.abs_files_in]
        self.files_out = [relpath(f, self.task_dir) for f in self.abs_files_out]
        self.deps = [relpath(f, self.task_dir) for f in self.abs_deps]

        # Now that files_in/files_out/deps are flat, we can expand our
        # description and command list
        self.command = expand_async(self, self.command)
        # pylint: disable=access-member-before-definition
        if self.desc:
            self.desc = expand_async(self, self.desc)
        if self.depfile:
            self.depfile = Path(expand_async(self, self.depfile))

    ########################################

    async def run_commands(self):
        """
        Runs all the commands in the task while holding the semaphore.
        """

        # OK, we're ready to start the task. Grab the semaphore before we start
        # printing status stuff so that it'll end up near the actual task
        # invocation.
        async with self.semaphore:

            # Deps fulfilled, we are now runnable so grab a task index.
            this.task_counter += 1
            self.task_index = this.task_counter

            # Print the "[1/N] Foo foo.foo foo.o" status line and debug information
            log(
                f"[{self.task_index}/{this.tasks_total}] {self.desc}",
                sameline=not self.verbose,
            )

            if self.verbose or self.debug:
                log(f"Reason: {self.reason}")
                for command in self.command:
                    log(f">>>{' (DRY RUN)' if self.dryrun else ''} {command}")
                if self.debug:
                    log(self)

            result = []
            with Chdir(self.task_dir):
                for command in self.command:
                    result = await self.run_command(command)

        this.tasks_pass += 1
        return result

    ########################################
    # Note - We should _not_ be expanding any templates in this step, that
    # should've been done already.

    async def run_command(self, command):
        """
        Runs a single command, either by calling it or running it in a subprocess.
        """

        # Early exit if this is just a dry run
        if self.dryrun:
            return self.abs_files_out

        # Custom commands just get called and then early-out'ed.
        if callable(command):
            with Chdir(self.task_dir):
                result = command(self)
            if result is None:
                raise ValueError(f"Command {command} returned None")
            return result

        # Non-string non-callable commands are not valid
        if not isinstance(command, str):
            raise ValueError(f"Don't know what to do with {command}")

        # Create the subprocess via asyncio and then await the result.
        with Chdir(self.task_dir):
            log(f"Running {command}")
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        (stdout_data, stderr_data) = await proc.communicate()

        self.stdout = stdout_data.decode()
        self.stderr = stderr_data.decode()
        self.returncode = proc.returncode

        # Print command output if needed
        if not self.quiet and (self.stdout or self.stderr):
            if self.stderr:
                log(self.stderr, end="")
            if self.stdout:
                log(self.stdout, end="")

        # Task complete, check the task return code
        if self.returncode:
            raise ValueError(
                f"Command '{command}' exited with return code {self.returncode}"
            )

        # Task passed, return the output file list
        return self.abs_files_out

    ########################################

    def needs_rerun(self, force=False):
        """
        Checks if a task needs to be re-run, and returns a non-empty reason if so.
        """

        # Pylint really doesn't like this function, lol.
        # pylint: disable=too-many-return-statements,too-many-branches

        files_in = self.abs_files_in
        files_out = self.abs_files_out

        if force:
            return f"Files {self.files_out} forced to rebuild"
        if not files_in:
            return "Always rebuild a target with no inputs"
        if not files_out:
            return "Always rebuild a target with no outputs"

        # Tasks with missing outputs always run.
        for file_out in files_out:
            if not file_out.exists():
                return f"Rebuilding {self.files_out} because some are missing"

        min_out = min(mtime(f) for f in files_out)

        # Check the hancho file(s) that generated the task
        if max(mtime(f) for f in this.hancho_mods.keys()) >= min_out:
            return f"Rebuilding {self.files_out} because its .hancho files have changed"

        # Check user-specified deps.
        if self.deps and max(mtime(f) for f in self.deps) >= min_out:
            return (
                f"Rebuilding {self.files_out} because a manual dependency has changed"
            )

        # Check depfile, if present.
        if self.depfile:
            abs_depfile = abspath(config.root_dir / self.depfile)
            if abs_depfile.exists():
                if self.debug:
                    log(f"Found depfile {abs_depfile}")
                with open(abs_depfile, encoding="utf-8") as depfile:
                    deplines = None
                    if self.depformat == "msvc":
                        # MSVC /sourceDependencies json depfile
                        deplines = json.load(depfile)["Data"]["Includes"]
                    elif self.depformat == "gcc":
                        # GCC .d depfile
                        deplines = depfile.read().split()
                        deplines = [d for d in deplines[1:] if d != "\\"]
                    else:
                        raise ValueError(f"Invalid depformat {self.depformat}")

                    if deplines and max(mtime(f) for f in deplines) >= min_out:
                        return (
                            f"Rebuilding {self.files_out} because a dependency in "
                            + f"{abs_depfile} has changed"
                        )

        # Check input files.
        if files_in and max(mtime(f) for f in files_in) >= min_out:
            return f"Rebuilding {self.files_out} because an input has changed"

        # All checks passed, so we don't need to rebuild this output.
        if self.debug:
            log(f"Files {self.files_out} are up to date")

        # All deps were up-to-date, nothing to do.
        return None


################################################################################

if __name__ == "__main__":
    sys.exit(main())
