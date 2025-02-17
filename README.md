# ![Logo](docs/hancho_small.png) Hancho

"班長, hanchō - "Squad leader”, from 19th c. Mandarin 班長 (bānzhǎng, “team leader”)"

Hancho is a simple, pleasant build system with few moving parts.

Hancho fits comfortably in a single Python file and requires no installation, just copy-paste it into your source tree.

Hancho is inspired by Ninja (for speed and simplicity) and Bazel (for syntax and extensibility).

Like Ninja, it knows nothing about your build tools and is only trying to assemble and run commands as fast as possible.

Unlike Ninja, you can use glob("*.cpp") and such to make things far less verbose.

Like Bazel, you invoke build rules by calling them as if they were functions with keyword arguments.

Unlike Bazel, you can create build rules that call arbitary Python code (for better or worse).

Hancho should suffice for small to medium sized projects.

[Tutorial Here](tutorial)

[Some Additional Documentation Here](docs)

## Updates

 - 2024-03-19 - Hancho v0.0.5
   - Special dir-related fields are now start_dir, root_dir, leaf_dir, work_dir, and build_dir
   - Hancho files in a submodule can be loaded via load(root="submodule/path", file="build.hancho")
   - Each Hancho module now gets its own 'config' object extended from its parent module (or global_config). This prevents submodules from accidentally changing global fields that their parent modules use while still allowing sharing of configuration across files.
 - 2024-03-13 - Tasks can now 'reserve' jobs so that commands that themselves use many jobs (like Ninja) can block until the jobs are free. See the [job_count](tests/job_count.hancho) test for details.
 - 2024-03-13 - Code cleaned up to be more standard Python style and reduce linter complaints. Added 'rule_dir' field to each Rule that stores the directory of the file that created the rule.
 - 2024-03-12 - Handling of paths is more flexible now (and will be documented shortly). Calling a Rule now returns a Task object. All the task-running code is now in Task instead of Rule.
 
## Installation

``` bash
user@host:~$ wget https://raw.githubusercontent.com/aappleby/hancho/main/hancho.py
user@host:~$ chmod +x hancho.py
user@host:~$ ./hancho.py --help
usage: hancho.py [-h] [-C CHDIR] [-j JOBS] [-v] [-q] [-n] [-d] [-f] [filename]

positional arguments:
  filename              The name of the .hancho file to build

options:
  -h, --help            show this help message and exit
  -C CHDIR, --chdir CHDIR
                        Change directory first
  -j JOBS, --jobs JOBS  Run N jobs in parallel (default = cpu_count)
  -v, --verbose         Print verbose build info
  -q, --quiet           Mute all output
  -n, --dryrun          Do not run commands
  -d, --debug           Print debugging information
  -f, --force           Force rebuild of everything
```

## Simple Example
```py
# examples/hello_world/build.hancho
from hancho import *

compile = Rule(
  desc      = "Compile {files_in} -> {files_out}",
  command   = "g++ -MMD -c {files_in} -o {files_out}",
  files_out = "{swap_ext(files_in, '.o')}",
  depfile   = "{swap_ext(files_out, '.d')}",
)

link = Rule(
  desc      = "Link {files_in} -> {files_out}",
  command   = "g++ {files_in} -o {files_out}",
)

main_o = compile("main.cpp")
main_app = link(main_o, "app")
```
```cpp
// examples/hello_world/main.cpp
#include <stdio.h>

int main(int argc, char** argv) {
  printf("Hello World\n");
  return 0;
}
```
```sh
user@host:~/hancho/examples/hello_world$ ../../hancho.py --verbose
[1/2] Compile main.cpp -> build/main.o
Reason: Rebuilding ['build/main.o'] because some are missing
g++ -MMD -c main.cpp -o build/main.o
[2/2] Link build/main.o -> build/app
Reason: Rebuilding ['build/app'] because some are missing
g++ build/main.o -o build/app
hancho: BUILD PASSED

user@host:~/hancho/examples/hello_world$ build/app
Hello World

user@host:~/hancho/examples/hello_world$ ../../hancho.py --verbose
hancho: BUILD CLEAN
```

## Old Updates

- 2024-03-07 - Tests should run on Windows now. Added a Windows build example. Promises are now valid as inputs to any template.
- 2024-03-04 - Cleaned up pylint & formatting issues in hancho.py and test.py. Hancho.py is now over 500 lines if you include whitespace and comments :D.
- 2024-03-04 - Unrecognized '--key=value' command line flags are now merged into the global config object. This allows you to do things like "hancho.py --build_dir=some/other/dir" which could be annoying otherwise.
- 2024-03-02 - Initial release. Some test cases yet to be written.
