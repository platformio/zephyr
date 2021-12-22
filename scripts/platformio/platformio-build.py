# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import subprocess
import shutil
import sys
import re
import tempfile

import click

from SCons.Script import ARGUMENTS, Builder, COMMAND_LINE_TARGETS

from platformio import fs
from platformio.compat import WINDOWS
from platformio.proc import exec_command
from platformio.util import get_systype
from platformio.package import version
from platformio.package.vcsclient import GitClient
from platformio.package.manager.tool import ToolPackageManager
from platformio.package.meta import PackageSpec


Import("env")


try:
    import yaml
    import pykwalify
except ImportError:
    deps = ["pyyaml", "pykwalify", "six"]
    if WINDOWS:
        deps.append("windows-curses")
    env.Execute(
        env.VerboseAction(
            "$PYTHONEXE -m pip install %s" % " ".join(deps),
            "Installing Zephyr's Python dependencies",
        )
    )

    import yaml


platform = env.PioPlatform()
board = env.BoardConfig()

FRAMEWORK_DIR = platform.get_package_dir("framework-zephyr")
FRAMEWORK_VERSION = platform.get_package_version("framework-zephyr")
assert os.path.isdir(FRAMEWORK_DIR)

BUILD_DIR = env.subst("$BUILD_DIR")
PROJECT_DIR = env.subst("$PROJECT_DIR")
PROJECT_SRC_DIR = env.subst("$PROJECT_SRC_DIR")
CMAKE_API_DIR = os.path.join(BUILD_DIR, ".cmake", "api", "v1")
CMAKE_API_QUERY_DIR = os.path.join(CMAKE_API_DIR, "query")
CMAKE_API_REPLY_DIR = os.path.join(CMAKE_API_DIR, "reply")

PLATFORMS_WITH_EXTERNAL_HAL = {
    "atmelsam": ["st", "atmel"],
    "chipsalliance": ["swervolf"],
    "freescalekinetis": ["st", "nxp"],
    "ststm32": ["st", "stm32"],
    "siliconlabsefm32": ["st", "silabs"],
    "nordicnrf51": ["st", "nordic"],
    "nordicnrf52": ["st", "nordic"],
    "nxplpc": ["st", "nxp"],
    "nxpimxrt": ["st", "nxp"],
    "teensy": ["st", "nxp"],
}


# By default Zephyr modules are cloned without submodules. Temporarily subclass
# the default PlatformIO Git client to override the clone command
class NonRecursiveGitClient(GitClient):
    def export(self):
        is_commit = self.is_commit_id(self.tag)
        args = ["clone"]
        if not self.tag or not is_commit:
            args += ["--depth", "1"]
            if self.tag:
                args += ["--branch", self.tag]
        args += [self.remote_url, self.src_dir]
        assert self.run_cmd(args, cwd=os.getcwd())
        if is_commit:
            assert self.run_cmd(["reset", "--hard", self.tag])
        return True


def get_board_architecture(board_config):
    if board_config.get("build.cpu", "").lower().startswith("cortex"):
        return "arm"
    elif board_config.get("build.march", "").startswith(("rv64", "rv32")):
        return "riscv"
    elif board_config.get("build.mcu") == "esp32":
        return "xtensa32"

    sys.stderr.write(
        "Error: Cannot configure Zephyr environment for %s\n"
        % env.subst("$PIOPLATFORM")
    )
    env.Exit(1)


def populate_zephyr_env_vars(zephyr_env, board_config):
    toolchain_variant = "UNKNOWN"
    arch = get_board_architecture(board_config)
    if arch == "arm":
        toolchain_variant = "gnuarmemb"
        zephyr_env["GNUARMEMB_TOOLCHAIN_PATH"] = platform.get_package_dir(
            "toolchain-gccarmnoneeabi"
        )
    elif arch == "riscv":
        toolchain_variant = "cross-compile"
        zephyr_env["CROSS_COMPILE"] = os.path.join(
            platform.get_package_dir("toolchain-riscv"), "bin", "riscv64-unknown-elf-"
        )
    elif arch == "xtensa32":
        toolchain_variant = "espressif"
        zephyr_env["ESPRESSIF_TOOLCHAIN_PATH"] = platform.get_package_dir(
            "toolchain-xtensa32"
        )

    zephyr_env["ZEPHYR_TOOLCHAIN_VARIANT"] = toolchain_variant
    zephyr_env["ZEPHYR_BASE"] = FRAMEWORK_DIR

    additional_packages = [
        platform.get_package_dir("tool-dtc"),
        platform.get_package_dir("tool-ninja"),
    ]

    if "windows" not in get_systype():
        additional_packages.append(platform.get_package_dir("tool-gperf"))

    zephyr_env["PATH"] = os.pathsep.join(additional_packages)


def is_proper_zephyr_project():
    return os.path.isfile(os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt"))


def create_default_project_files():
    cmake_tpl = """cmake_minimum_required(VERSION 3.13.1)
include($ENV{ZEPHYR_BASE}/cmake/app/boilerplate.cmake NO_POLICY_SCOPE)
project(%s)

FILE(GLOB app_sources ../src/*.c*)
target_sources(app PRIVATE ${app_sources})
"""

    app_tpl = """#include <zephyr.h>

void main(void)
{
}
"""

    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    if not os.path.isfile(cmake_txt_file):
        os.makedirs(os.path.dirname(cmake_txt_file))
        with open(cmake_txt_file, "w") as fp:
            fp.write(cmake_tpl % os.path.basename(PROJECT_DIR))

    if not os.listdir(os.path.join(PROJECT_SRC_DIR)):
        # create an empty file to make CMake happy during first init
        with open(os.path.join(PROJECT_SRC_DIR, "main.c"), "w") as fp:
            fp.write(app_tpl)


def is_cmake_reconfigure_required():
    cmake_cache_file = os.path.join(BUILD_DIR, "CMakeCache.txt")
    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    cmake_preconf_dir = os.path.join(BUILD_DIR, "zephyr", "include", "generated")
    cmake_preconf_misc = os.path.join(BUILD_DIR, "zephyr", "misc", "generated")
    zephyr_prj_conf = os.path.join(PROJECT_DIR, "zephyr", "prj.conf")

    for d in (CMAKE_API_REPLY_DIR, cmake_preconf_dir, cmake_preconf_misc):
        if not os.path.isdir(d) or not os.listdir(d):
            return True
    if not os.path.isfile(cmake_cache_file):
        return True
    if not os.path.isfile(os.path.join(BUILD_DIR, "build.ninja")):
        return True
    if os.path.getmtime(cmake_txt_file) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.isfile(zephyr_prj_conf) and os.path.getmtime(
        zephyr_prj_conf
    ) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.getmtime(FRAMEWORK_DIR) > os.path.getmtime(cmake_cache_file):
        return True

    return False


def run_cmake(manifest):
    print("Reading CMake configuration...")

    CONFIG_PATH = board.get(
        "build.zephyr.config_path",
        os.path.join(PROJECT_DIR, "config.%s" % env.subst("$PIOENV")),
    )

    cmake_cmd = [
        os.path.join(platform.get_package_dir("tool-cmake") or "", "bin", "cmake"),
        "-S",
        os.path.join(PROJECT_DIR, "zephyr"),
        "-B",
        BUILD_DIR,
        "-G",
        "Ninja",
        "-DBOARD=%s" % get_zephyr_target(board),
        "-DPYTHON_EXECUTABLE:FILEPATH=%s" % env.subst("$PYTHONEXE"),
        "-DPYTHON_PREFER:FILEPATH=%s" % env.subst("$PYTHONEXE"),
        "-DPIO_PACKAGES_DIR:PATH=%s" % env.subst("$PROJECT_PACKAGES_DIR"),
        "-DDOTCONFIG=" + CONFIG_PATH,
    ]

    menuconfig_file = os.path.join(PROJECT_DIR, "zephyr", "menuconfig.conf")
    if os.path.isfile(menuconfig_file):
        print("Adding -DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)
        cmake_cmd.append("-DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)

    if board.get("build.zephyr.cmake_extra_args", ""):
        cmake_cmd.extend(
            click.parser.split_arg_string(board.get("build.zephyr.cmake_extra_args"))
        )

    modules = [generate_default_component()]

    for project in manifest.get("projects", []):
        if not is_project_required(project):
            continue

        modules.append(
            fs.to_unix_path(
                os.path.join(
                    FRAMEWORK_DIR,
                    "_pio",
                    project["path"] if "path" in project else project["name"],
                )
            )
        )

    cmake_cmd.extend(["-D", "ZEPHYR_MODULES=" + ";".join(modules)])

    # Run Zephyr in an isolated environment with specific env vars
    zephyr_env = os.environ.copy()
    populate_zephyr_env_vars(zephyr_env, board)

    result = exec_command(cmake_cmd, env=zephyr_env)
    if result["returncode"] != 0:
        sys.stderr.write(result["out"] + "\n")
        sys.stderr.write(result["err"])
        env.Exit(1)

    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print(result["out"])
        print(result["err"])


def get_cmake_code_model(manifest):
    if not is_proper_zephyr_project():
        create_default_project_files()

    if is_cmake_reconfigure_required():
        # Explicitly clean build folder to avoid cached values
        if os.path.isdir(CMAKE_API_DIR):
            fs.rmtree(BUILD_DIR)
        query_file = os.path.join(CMAKE_API_QUERY_DIR, "codemodel-v2")
        if not os.path.isfile(query_file):
            os.makedirs(os.path.dirname(query_file))
            open(query_file, "a").close()  # create an empty file
        run_cmake(manifest)

    if not os.path.isdir(CMAKE_API_REPLY_DIR) or not os.listdir(CMAKE_API_REPLY_DIR):
        sys.stderr.write("Error: Couldn't find CMake API response file\n")
        env.Exit(1)

    codemodel = {}
    for target in os.listdir(CMAKE_API_REPLY_DIR):
        if target.startswith("codemodel-v2"):
            with open(os.path.join(CMAKE_API_REPLY_DIR, target), "r") as fp:
                codemodel = json.load(fp)

    assert codemodel["version"]["major"] == 2
    return codemodel


def get_zephyr_target(board_config):
    return board_config.get("build.zephyr.variant", env.subst("$BOARD").lower())


def get_target_elf_arch(board_config):
    architecture = get_board_architecture(board_config)
    if architecture == "arm":
        return "elf32-littlearm"
    if architecture == "riscv":
        if board.get("build.march", "") == "rv32":
            return "elf32-littleriscv"
        return "elf64-littleriscv"
    if architecture == "xtensa32":
        return "elf32-xtensa-le"

    sys.stderr.write(
        "Error: Cannot find correct elf architecture for %s\n"
        % env.subst("$PIOPLATFORM")
    )
    env.Exit(1)


def build_library(default_env, lib_config, project_src_dir, prepend_dir=None):
    lib_name = lib_config.get("nameOnDisk", lib_config["name"])
    lib_path = lib_config["paths"]["build"]
    if prepend_dir:
        lib_path = os.path.join(prepend_dir, lib_path)
    lib_objects = compile_source_files(
        lib_config, default_env, project_src_dir, prepend_dir
    )

    return default_env.Library(
        target=os.path.join("$BUILD_DIR", lib_path, lib_name), source=lib_objects
    )


def get_target_config(project_configs, target_index):
    target_json = project_configs.get("targets")[target_index].get("jsonFile", "")
    target_config_file = os.path.join(CMAKE_API_REPLY_DIR, target_json)
    if not os.path.isfile(target_config_file):
        sys.stderr.write("Error: Couldn't find target config %s\n" % target_json)
        env.Exit(1)

    with open(target_config_file) as fp:
        return json.load(fp)


def _fix_package_path(module_path):
    # Possible package names in 'package@version' format is not compatible with CMake
    module_name = os.path.basename(module_path)
    if "@" in module_name:
        new_path = os.path.join(
            os.path.dirname(module_path),
            module_name.replace("@", "-"),
        )
        os.rename(module_path, new_path)
        module_path = new_path

    assert module_path and os.path.isdir(module_path)
    return module_path


def generate_includible_file(source_file):
    cmd = [
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "file2hex.py"),
        "--file",
        "$SOURCE",
        ">",
        "$TARGET",
    ]

    return env.Command(
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "${SOURCE.file}.inc"
        ),
        env.File(source_file),
        env.VerboseAction(" ".join(cmd), "Generating file $TARGET"),
    )


def generate_kobject_files():
    kobj_files = (
        os.path.join("$BUILD_DIR", "zephyr", "include", "generated", f)
        for f in ("kobj-types-enum.h", "otype-to-str.h", "otype-to-size.h")
    )

    if all(os.path.isfile(env.subst(f)) for f in kobj_files):
        return

    cmd = (
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "gen_kobject_list.py"),
        "--kobj-types-output",
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "kobj-types-enum.h"
        ),
        "--kobj-otype-output",
        os.path.join("$BUILD_DIR", "zephyr", "include", "generated", "otype-to-str.h"),
        "--kobj-size-output",
        os.path.join("$BUILD_DIR", "zephyr", "include", "generated", "otype-to-size.h"),
        "--include",
        os.path.join("$BUILD_DIR", "zephyr", "misc", "generated", "struct_tags.json"),
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating KObject files..."))


def validate_driver():

    driver_header = os.path.join(
        "$BUILD_DIR", "zephyr", "include", "generated", "driver-validation.h"
    )

    if os.path.isfile(env.subst(driver_header)):
        return

    cmd = (
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "gen_kobject_list.py"),
        "--validation-output",
        driver_header,
        "--include",
        os.path.join("$BUILD_DIR", "zephyr", "misc", "generated", "struct_tags.json"),
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Validating driver..."))


def generate_dev_handles(preliminary_elf_path):
    cmd = (
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "gen_handles.py"),
        "--output-source",
        "$TARGET",
        "--kernel",
        "$SOURCE",
        "--start-symbol",
        "__device_start",
        "--zephyr-base",
        FRAMEWORK_DIR,
    )

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "dev_handles.c"),
        preliminary_elf_path,
        env.VerboseAction(" ".join(cmd), "Generating $TARGET"),
    )


def parse_syscalls():
    syscalls_config = os.path.join(
        "$BUILD_DIR", "zephyr", "misc", "generated", "syscalls.json"
    )

    struct_tags = os.path.join(
        "$BUILD_DIR", "zephyr", "misc", "generated", "struct_tags.json"
    )

    if not all(os.path.isfile(env.subst(f)) for f in (syscalls_config, struct_tags)):
        cmd = [
            "$PYTHONEXE",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "parse_syscalls.py"),
            "--include",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "include"),
            "--include",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "drivers"),
            "--include",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "subsys", "net"),
        ]

        # Temporarily until CMake exports actual custom commands
        if board.get("build.zephyr.syscall_include_dirs", ""):
            incs = [
                inc if os.path.isabs(inc) else os.path.join(PROJECT_DIR, inc)
                for inc in board.get("build.zephyr.syscall_include_dirs").split()
            ]

            cmd.extend(['--include "%s"' % inc for inc in incs])

        cmd.extend(("--json-file", syscalls_config, "--tag-struct-file", struct_tags))

        env.Execute(env.VerboseAction(" ".join(cmd), "Parsing system calls..."))

    return syscalls_config


def generate_syscall_files(syscalls_json, project_settings):
    syscalls_header = os.path.join(
        BUILD_DIR, "zephyr", "include", "generated", "syscall_list.h"
    )

    if os.path.isfile(syscalls_header):
        return

    cmd = [
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "gen_syscalls.py"),
        "--json-file",
        syscalls_json,
        "--base-output",
        os.path.join("$BUILD_DIR", "zephyr", "include", "generated", "syscalls"),
        "--syscall-dispatch",
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "syscall_dispatch.c"
        ),
        "--syscall-list",
        syscalls_header,
    ]

    if project_settings.get("CONFIG_TIMEOUT_64BIT", False) == "1":
        cmd.extend(("--split-type", "k_timeout_t"))

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating syscall files"))


def get_linkerscript_final_cmd(app_includes, base_ld_script):
    cmd = [
        "$CC",
        "-x",
        "assembler-with-cpp",
        "-undef",
        "-MD",
        "-MF",
        "${TARGET}.dep",
        "-MT",
        "$TARGET",
        "-D__GCC_LINKER_CMD__",
        "-DLINKER_PASS2",
        "-D_LINKER",
        "-D_ASMLANGUAGE",
        "-DLINKER_ZEPHYR_FINAL",
        "-E",
        "$SOURCE",
        "-P",
        "-o",
        "$TARGET",
    ]

    cmd.extend(['-I"%s"' % inc for inc in app_includes["plain_includes"]])

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "linker.cmd"),
        base_ld_script,
        env.VerboseAction(" ".join(cmd), "Generating final linker script $TARGET"),
    )


def find_base_ldscript(app_includes):
    # A temporary solution since there is no easy way to find linker script
    for inc in app_includes["plain_includes"]:
        for f in os.listdir(inc):
            if f == "linker.ld" and os.path.isfile(os.path.join(inc, f)):
                return os.path.join(inc, f)

    sys.stderr.write("Error: Couldn't find a base linker script!\n")
    env.Exit(1)


def get_linkerscript_cmd(app_includes, base_ld_script):
    cmd = [
        "$CC",
        "-x",
        "assembler-with-cpp",
        "-undef",
        "-MD",
        "-MF",
        "${TARGET}.dep",
        "-MT",
        "$TARGET",
        "-D__GCC_LINKER_CMD__",
        "-D_LINKER",
        "-D_ASMLANGUAGE",
        "-DLINKER_ZEPHYR_PREBUILT",
        "-E",
        "$SOURCE",
        "-P",
        "-o",
        "$TARGET",
    ]

    cmd.extend(['-I"%s"' % inc for inc in app_includes["plain_includes"]])

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "linker_zephyr_prebuilt.cmd"),
        base_ld_script,
        env.VerboseAction(" ".join(cmd), "Generating linker script $TARGET"),
    )


def load_target_configurations(cmake_codemodel):
    configs = {}
    project_configs = cmake_codemodel.get("configurations")[0]
    for config in project_configs.get("projects", []):
        for target_index in config.get("targetIndexes", []):
            target_config = get_target_config(project_configs, target_index)
            configs[target_config["name"]] = target_config

    return configs


def extract_defines_from_compile_group(compile_group):
    result = []
    result.extend(
        [
            d.get("define").replace('"', '\\"').strip()
            for d in compile_group.get("defines", [])
        ]
    )

    for f in compile_group.get("compileCommandFragments", []):
        result.extend(env.ParseFlags(f.get("fragment", "")).get("CPPDEFINES", []))
    return result


def prepare_build_envs(config, default_env):
    build_envs = []
    target_compile_groups = config.get("compileGroups", [])
    is_build_type_debug = (
        set(["debug", "sizedata"]) & set(COMMAND_LINE_TARGETS)
        or default_env.GetProjectOption("build_type") == "debug"
    )

    for cg in target_compile_groups:
        includes = extract_includes_from_compile_group(cg, path_prefix=FRAMEWORK_DIR)
        defines = extract_defines_from_compile_group(cg)
        build_env = default_env.Clone()
        compile_commands = cg.get("compileCommandFragments", [])

        i = 0
        length = len(compile_commands)
        while i < length:
            build_flags = compile_commands[i].get("fragment", "")
            if build_flags.strip() in ("-imacros", "-include"):
                i += 1
                file_path = compile_commands[i].get("fragment", "")
                build_env.Append(CCFLAGS=[build_flags + file_path])
            elif build_flags.strip() and not build_flags.startswith("-D"):
                build_env.AppendUnique(**build_env.ParseFlags(build_flags))
            i += 1
        build_env.AppendUnique(CPPDEFINES=defines, CPPPATH=includes["plain_includes"])
        if includes["prefixed_includes"]:
            build_env.Append(CCFLAGS=["-iprefix", fs.to_unix_path(FRAMEWORK_DIR)])
            build_env.Append(
                CCFLAGS=[
                    "-iwithprefixbefore/" + inc for inc in includes["prefixed_includes"]
                ]
            )
        if includes["sys_includes"]:
            build_env.Append(
                CCFLAGS=["-isystem" + inc for inc in includes["sys_includes"]]
            )
        build_env.Append(ASFLAGS=build_env.get("CCFLAGS", [])[:])
        build_env.ProcessUnFlags(default_env.get("BUILD_UNFLAGS"))
        if is_build_type_debug:
            build_env.ConfigureDebugFlags()
        build_envs.append(build_env)

    return build_envs


def compile_source_files(config, default_env, project_src_dir, prepend_dir=None):
    build_envs = prepare_build_envs(config, default_env)
    objects = []
    for source in config.get("sources", []):
        if source["path"].endswith(".rule"):
            continue
        compile_group_idx = source.get("compileGroupIndex")
        if compile_group_idx is not None:
            src_path = source.get("path")
            if not os.path.isabs(src_path):
                # For cases when sources are located near CMakeLists.txt
                src_path = os.path.join(PROJECT_DIR, "zephyr", src_path)
            local_path = config["paths"]["source"]
            if not os.path.isabs(local_path):
                local_path = os.path.join(project_src_dir, config["paths"]["source"])
            obj_path_temp = os.path.join(
                "$BUILD_DIR",
                prepend_dir or config["name"].replace("framework-zephyr", ""),
                config["paths"]["build"],
            )
            if src_path.startswith(local_path):
                obj_path = os.path.join(
                    obj_path_temp, os.path.relpath(src_path, local_path)
                )
            else:
                obj_path = os.path.join(obj_path_temp, os.path.basename(src_path))
            objects.append(
                build_envs[compile_group_idx].StaticObject(
                    target=os.path.join(obj_path + ".o"),
                    source=os.path.realpath(src_path),
                )
            )

    return objects


def get_app_includes(app_config):
    includes = extract_includes_from_compile_group(app_config["compileGroups"][0])
    return includes


def extract_includes_from_compile_group(compile_group, path_prefix=None):
    def _normalize_prefix(prefix):
        prefix = fs.to_unix_path(prefix)
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        return prefix

    if path_prefix:
        path_prefix = _normalize_prefix(path_prefix)

    includes = []
    sys_includes = []
    prefixed_includes = []
    for inc in compile_group.get("includes", []):
        inc_path = fs.to_unix_path(inc["path"])
        if inc.get("isSystem", False):
            sys_includes.append(inc_path)
        elif path_prefix and inc_path.startswith(path_prefix):
            prefixed_includes.append(
                fs.to_unix_path(os.path.relpath(inc_path, path_prefix))
            )
        else:
            includes.append(inc_path)

    return {
        "plain_includes": includes,
        "sys_includes": sys_includes,
        "prefixed_includes": prefixed_includes,
    }


def get_app_defines(app_config):
    return extract_defines_from_compile_group(app_config["compileGroups"][0])


def extract_link_args(target_config):
    link_args = {
        "link_flags": [],
        "lib_paths": [],
        "project_libs": {"whole_libs": [], "generic_libs": [], "standard_libs": []},
    }

    is_whole_archive = False
    for f in target_config.get("link", {}).get("commandFragments", []):
        fragment = f.get("fragment", "").strip().replace("\\", "/")
        fragment_role = f.get("role", "").strip()
        if not fragment or not fragment_role:
            continue
        args = click.parser.split_arg_string(fragment)
        if "-Wl,--whole-archive" in fragment:
            is_whole_archive = True
        if "-Wl,--no-whole-archive" in fragment:
            is_whole_archive = False
        if fragment_role == "flags":
            link_args["link_flags"].extend(args)
        elif fragment_role == "libraries":
            if fragment.startswith(("-l", "-Wl,-l")):
                link_args["project_libs"]["standard_libs"].extend(args)
            elif fragment.startswith("-L"):
                lib_path = fragment.replace("-L", "").strip()
                if lib_path not in link_args["lib_paths"]:
                    link_args["lib_paths"].append(lib_path.replace('"', ""))
            elif fragment.startswith("-") and not fragment.startswith("-l"):
                # CMake mistakenly marks link_flags as libraries
                link_args["link_flags"].extend(args)
            elif os.path.isfile(fragment) and os.path.isabs(fragment):
                # In case of precompiled archives from framework package
                lib_path = os.path.dirname(fragment)
                if lib_path not in link_args["lib_paths"]:
                    link_args["lib_paths"].append(os.path.dirname(fragment))
                link_args["project_libs"]["standard_libs"].extend(
                    [os.path.basename(lib) for lib in args if lib.endswith(".a")]
                )
            elif fragment.endswith(".a"):
                link_args["project_libs"][
                    "whole_libs" if is_whole_archive else "generic_libs"
                ].extend([lib.replace("\\", "/") for lib in args if lib.endswith(".a")])
            else:
                link_args["link_flags"].extend(args)

    return link_args


def generate_isr_list_binary(preliminary_elf, board):
    cmd = [
        "$OBJCOPY",
        "--input-target=" + get_target_elf_arch(board),
        "--output-target=binary",
        "--only-section=.intList",
        "$SOURCE",
        "$TARGET",
    ]

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "isrList.bin"),
        preliminary_elf,
        env.VerboseAction(" ".join(cmd), "Generating ISR list $TARGET"),
    )


def generate_isr_table_file_cmd(preliminary_elf, board_config, project_settings):
    cmd = [
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "arch", "common", "gen_isr_tables.py"),
        "--output-source",
        "$TARGET",
        "--kernel",
        "${SOURCES[0]}",
        "--intlist",
        "${SOURCES[1]}",
    ]

    if project_settings.get("CONFIG_GEN_ISR_TABLES", "") == "y":
        cmd.append("--sw-isr-table")
    if project_settings.get("CONFIG_GEN_IRQ_VECTOR_TABLE", "") == "y":
        cmd.append("--vector-table")

    cmd = env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "isr_tables.c"),
        [preliminary_elf, os.path.join("$BUILD_DIR", "zephyr", "isrList.bin")],
        env.VerboseAction(" ".join(cmd), "Generating ISR table $TARGET"),
    )

    env.Requires(cmd, generate_isr_list_binary(preliminary_elf, board_config))

    return cmd


def generate_offset_header_file_cmd():
    cmd = [
        "$PYTHONEXE",
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "gen_offset_header.py"),
        "-i",
        "$SOURCE",
        "-o",
        "$TARGET",
    ]

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "include", "generated", "offsets.h"),
        os.path.join(
            "$BUILD_DIR",
            "offsets",
            "zephyr",
            "arch",
            get_board_architecture(board),
            "core",
            "offsets",
            "offsets.c.o",
        ),
        env.VerboseAction(" ".join(cmd), "Generating header file with offsets $TARGET"),
    )


def filter_args(args, allowed, ignore=None):
    if not allowed:
        return []

    ignore = ignore or []
    result = []
    i = 0
    length = len(args)
    while i < length:
        if any(args[i].startswith(f) for f in allowed) and not any(
            args[i].startswith(f) for f in ignore
        ):
            result.append(args[i])
            if i + 1 < length and not args[i + 1].startswith("-"):
                i += 1
                result.append(args[i])
        i += 1
    return result


def load_project_settings():
    result = {}
    config_re = re.compile(r"^([^#=]+)=(.+)$")
    config_file = os.path.join(BUILD_DIR, "zephyr", ".config")
    if not os.path.isfile(config_file):
        print("Warning! Missing project configuration file `%s`" % config_file)
        return {}

    with open(config_file) as f:
        for line in f:
            re_match = config_re.match(line)
            if re_match:
                config_value = re_match.group(2)
                if config_value.startswith('"') and config_value.endswith('"'):
                    config_value = config_value[1:-1]
                result[re_match.group(1)] = config_value

    return result


def RunMenuconfig(target, source, env):
    zephyr_env = os.environ.copy()
    populate_zephyr_env_vars(zephyr_env, board)

    rc = subprocess.call(
        [
            os.path.join(platform.get_package_dir("tool-cmake"), "bin", "cmake"),
            "--build",
            BUILD_DIR,
            "--target",
            "menuconfig",
        ],
        env=zephyr_env,
    )

    if rc != 0:
        sys.stderr.write("Error: Couldn't execute 'menuconfig' target.\n")
        env.Exit(1)


def get_project_lib_deps(modules_map, main_config):
    def _collect_lib_deps(config, libs=None):
        libs = libs or {}
        deps = config.get("dependencies", [])
        if not deps:
            return []

        for d in config["dependencies"]:
            dependency_id = d["id"]
            if not modules_map.get(dependency_id, {}):
                continue
            if dependency_id not in libs:
                libs[dependency_id] = modules_map[dependency_id]
                _collect_lib_deps(libs[dependency_id]["config"], libs)

        return libs

    return _collect_lib_deps(main_config)


def load_west_manifest(manifest_path):
    if not os.path.isfile(manifest_path):
        sys.stderr.write("Error: Couldn't find `%s`\n" % manifest_path)
        env.Exit(1)

    with open(manifest_path) as fp:
        try:
            return yaml.safe_load(fp).get("manifest", {})
        except yaml.YAMLError as e:
            sys.stderr.write("Warning! Failed to parse `%s`.\n" % manifest_path)
            sys.stderr.write(str(e) + "\n")
            env.Exit(1)


def prepare_package_url(remotes, default_remote_name, package_config):
    if "url" in package_config:
        remote_url = package_config["url-base"]
    else:
        remote_url = remotes.get(default_remote_name, "").get("url-base", "")
        if "remote" in package_config:
            remote_url = remotes[package_config["remote"]]["url-base"]

        remote_url = (
            remote_url
            + "/"
            + (
                package_config["repo-path"]
                if "repo-path" in package_config
                else package_config["name"]
            )
        )

    return remote_url + ".git"


def get_package_requirement(package_config):
    package_revision = package_config["revision"]
    # At least 10 symbols are required in commit hash
    hash_re = re.compile(r"[0-9a-f]{10,40}")
    if hash_re.match(package_revision):
        return "0.0.0-alpha+sha.%s" % package_revision[:10]
    elif package_revision.startswith("v") and "." in package_revision:
        # Remove 'v' and try to get a valid semver version
        try:
            v = version.cast_version_to_semver(
                package_revision[1:], raise_exception=True
            )
            return str(v)
        except:
            pass

    return None


def install_from_remote(package_config, dst_dir, remotes, default_remote):
    remote_url = prepare_package_url(remotes, default_remote, package_config)
    revision = package_config["revision"] if "revision" in package_config else "master"
    if not env.WhereIs("git"):
        print(
            "Warning! Git client is not installed in your system! "
            "Install Git client from https://git-scm.com/downloads and try again."
        )
        return

    os.makedirs(dst_dir)
    vcs = None
    if package_config.get("submodules", False):
        vcs = GitClient(fs.to_unix_path(dst_dir), remote_url, revision, True)
    else:
        vcs = NonRecursiveGitClient(
            fs.to_unix_path(dst_dir), remote_url, revision, True
        )
    assert vcs.export()


def install_from_registry(project_config, package_manager, package_path):
    package_name = "framework-zephyr-%s" % project_config["name"].replace("_", "-")
    package_requirement = get_package_requirement(project_config)
    spec = PackageSpec(
        owner="platformio",
        name=package_name,
        requirements=package_requirement,
    )
    try:
        pkg = package_manager.install(spec, silent=True)
        if os.path.isdir(pkg.path):
            if not os.path.isdir(os.path.dirname(package_path)):
                os.makedirs(os.path.dirname(package_path))
            # Move the folder to proper location in the Zephyr package
            shutil.move(pkg.path, package_path)
            assert os.path.isdir(package_path)
            return package_path
    except Exception:
        print(
            "Couldn't install the `%s` package from PlatformIO Registry." % package_name
        )

    return None


def process_bundled_packages(west_manifest):
    if not west_manifest:
        print("Warning! Empty package manifest!")

    assert (
        "projects" in west_manifest
    ), "Missing the `projects` field in package manifest!"

    # Create a folder for extra packages from west.yml
    packages_root = os.path.join(FRAMEWORK_DIR, "_pio")
    if not os.path.isdir(packages_root):
        os.makedirs(packages_root)

    # Remotes for external Zephyr packages if installed from repository
    default_remote = west_manifest.get("defaults", {}).get("remote", "")
    remotes = {remote["name"]: remote for remote in west_manifest["remotes"]}

    with tempfile.TemporaryDirectory(prefix="_pio") as tmpdir:
        # Built-in PlatformIO Package manager to download remote packages
        pm = ToolPackageManager(tmpdir)
        # Install missing packages
        for project_config in west_manifest.get("projects", []):
            if not is_project_required(project_config):
                continue

            project_name = project_config["name"]
            package_path = os.path.join(
                packages_root, project_config.get("path", project_name)
            )
            if not os.path.isdir(package_path):
                if project_name == "trusted-firmware-m":
                    # Support for this module is not implemented
                    continue
                print("Installing `%s` package..." % project_name)
                if not install_from_registry(project_config, pm, package_path):
                    install_from_remote(
                        project_config, package_path, remotes, default_remote
                    )


def generate_default_component():
    # Used to force CMake generate build environments for all supported languages

    prj_cmake_tpl = """# Warning! Do not delete this auto-generated file.
file(GLOB module_srcs *.c* *.S)
add_library(_PIODUMMY INTERFACE)
zephyr_library()
zephyr_library_sources(${module_srcs})
"""

    module_cfg_tpl = """# Warning! Do not delete this auto-generated file.
build:
  cmake: .
"""

    dummy_component_path = os.path.join(FRAMEWORK_DIR, "_pio", "_bare_module")
    if not os.path.isdir(dummy_component_path):
        os.makedirs(dummy_component_path)

    for ext in (".cpp", ".c", ".S"):
        dummy_src_file = os.path.join(dummy_component_path, "__dummy" + ext)
        if not os.path.isfile(dummy_src_file):
            open(dummy_src_file, "a").close()

    component_cmake = os.path.join(dummy_component_path, "CMakeLists.txt")
    if not os.path.isfile(component_cmake):
        with open(component_cmake, "w") as fp:
            fp.write(prj_cmake_tpl)

    zephyr_module_config = os.path.join(dummy_component_path, "zephyr", "module.yml")
    if not os.path.isfile(zephyr_module_config):
        if not os.path.isdir(zephyr_module_config):
            os.makedirs(os.path.dirname(zephyr_module_config))
        with open(zephyr_module_config, "w") as fp:
            fp.write(module_cfg_tpl)

    return dummy_component_path


def get_default_build_flags(app_config, default_config):
    assert default_config

    def _extract_flags(config):
        flags = {}
        for cg in config.get("compileGroups", []):
            flags[cg["language"]] = []
            for ccfragment in cg["compileCommandFragments"]:
                fragment = ccfragment.get("fragment", "")
                if not fragment.strip() or fragment.startswith("-D"):
                    continue
                flags[cg["language"]].extend(
                    click.parser.split_arg_string(fragment.strip())
                )

        return flags

    app_flags = _extract_flags(app_config)
    default_flags = _extract_flags(default_config)

    return {
        "ASFLAGS": app_flags.get("ASM", default_flags.get("ASM")),
        "CFLAGS": app_flags.get("C", default_flags.get("C")),
        "CXXFLAGS": app_flags.get("CXX", default_flags.get("CXX")),
    }


def is_project_required(project_config):
    # Some packages are not
    project_name = project_config["name"]
    if project_name.startswith("hal_") and project_name[
        4:
    ] not in PLATFORMS_WITH_EXTERNAL_HAL.get(env.subst("$PIOPLATFORM"), []):
        return False

    if project_config["path"].startswith("tool") or project_name.startswith("nrf_hw_"):
        return False

    return True


def get_default_module_config(target_configs):
    for config in target_configs:
        if "_pio___bare_module" in config:
            return target_configs[config]
    return {}


def process_project_lib_deps(
    modules_map, project_libs, preliminary_elf_path, offset_lib, lib_paths
):
    # Get rid of the `app` library as the project source files are handled by PlatformIO
    # and linker as object files in the linker command
    whole_libs = [
        lib for lib in project_libs["whole_libs"] if "app.a" not in lib
    ]

    # Some of the project libraries should be linked entirely, so they are manually
    # wrapped inside the `--whole-archive` and `--no-whole-archive` flags.
    env.Append(
        LIB_PATH=lib_paths,
        _LIBFLAGS=" -Wl,--whole-archive "
        + " ".join(
            [os.path.join("$BUILD_DIR", library) for library in whole_libs]
            + [offsets_lib[0].get_abspath()]
        )
        + " -Wl,--no-whole-archive "
        + " ".join(
            [
                os.path.join("$BUILD_DIR", library)
                for library in project_libs["generic_libs"]
            ]
            + project_libs["standard_libs"]
        ),
    )

    # Note: These libraries are not added to the `LIBS` section. Hence they must be
    # specified as explicit dependencies.
    env.Depends(
        preliminary_elf_path,
        [
            os.path.join("$BUILD_DIR", library)
            for library in project_libs["generic_libs"] + whole_libs
            if "app" not in library
        ],
    )


#
# Current build script limitations
#

env.EnsurePythonVersion(3, 4)

if " " in FRAMEWORK_DIR:
    sys.stderr.write("Error: Detected a whitespace character in framework path\n")
    env.Exit(1)

#
# Process Zephyr internal packages
#

west_manifest = load_west_manifest(os.path.join(FRAMEWORK_DIR, "west.yml"))
process_bundled_packages(west_manifest)

#
# Initial targets loading
#

codemodel = get_cmake_code_model(west_manifest)
if not codemodel:
    sys.stderr.write("Error: Couldn't find code model generated by CMake\n")
    env.Exit(1)

target_configs = load_target_configurations(codemodel)

app_config = target_configs.get("app")
prebuilt_config = target_configs.get("zephyr_prebuilt")

if not app_config or not prebuilt_config:
    sys.stderr.write("Error: Couldn't find main Zephyr target in the code model\n")
    env.Exit(1)

project_settings = load_project_settings()

#
# Generate prerequisite files
#

offset_header_file = generate_offset_header_file_cmd()
syscalls_config = parse_syscalls()
generate_syscall_files(syscalls_config, project_settings)
generate_kobject_files()
validate_driver()

#
# LD scripts processing
#

app_includes = get_app_includes(app_config)
base_ld_script = find_base_ldscript(app_includes)
final_ld_script = get_linkerscript_final_cmd(app_includes, base_ld_script)
preliminary_ld_script = get_linkerscript_cmd(app_includes, base_ld_script)

env.Depends(final_ld_script, offset_header_file)
env.Depends(preliminary_ld_script, offset_header_file)

#
# Includible files processing
#

if (
    "generate_inc_file_for_target"
    in app_config.get("backtraceGraph", {}).get("commands", [])
    and "build.embed_files" not in board
):
    print(
        "Warning! Detected a custom CMake command for embedding files. Please use "
        "'board_build.embed_files' option in 'platformio.ini' to include files!"
    )

if "build.embed_files" in board:
    for f in board.get("build.embed_files", "").split():
        file = os.path.join(PROJECT_DIR, f)
        if not os.path.isfile(env.subst(f)):
            print('Warning! Could not find file "%s"' % os.path.basename(f))
            continue

        env.Depends(offset_header_file, generate_includible_file(file))

#
# Libraries processing
#

env.Append(CPPDEFINES=[("BUILD_VERSION", "zephyr-v" + FRAMEWORK_VERSION.split(".")[1])])

framework_modules_map = {}
for target, target_config in target_configs.items():
    lib_name = target_config["name"]
    if (
        target_config["type"]
        not in (
            "STATIC_LIBRARY",
            "OBJECT_LIBRARY",
        )
        or lib_name in ("app", "offsets")
    ):
        continue

    lib = build_library(env, target_config, PROJECT_SRC_DIR)
    framework_modules_map[target_config["id"]] = {
        "lib_path": lib[0],
        "config": target_config,
    }

    if any(
        d.get("id", "").startswith("zephyr_generated_headers")
        for d in target_config.get("dependencies", [])
    ):
        env.Depends(lib[0].sources, offset_header_file)

# Offsets library compiled separately as it's used later for custom dependencies
offsets_lib = build_library(env, target_configs["offsets"], PROJECT_SRC_DIR)

#
# Preliminary elf and subsequent targets
#

preliminary_elf_path = os.path.join("$BUILD_DIR", "zephyr", "firmware-pre.elf")

for dep in (offsets_lib, preliminary_ld_script):
    env.Depends(preliminary_elf_path, dep)

isr_table_file = generate_isr_table_file_cmd(
    preliminary_elf_path, board, project_settings
)
if project_settings.get("CONFIG_HAS_DTS", ""):
    dev_handles = generate_dev_handles(preliminary_elf_path)

#
# Final firmware targets
#

env.Append(
    PIOBUILDFILES=compile_source_files(prebuilt_config, env, PROJECT_SRC_DIR),
    _EXTRA_ZEPHYR_PIOBUILDFILES=compile_source_files(
        target_configs["zephyr_final"], env, PROJECT_SRC_DIR
    ),
    __ZEPHYR_OFFSET_HEADER_CMD=offset_header_file,
)

for dep in (isr_table_file, final_ld_script):
    env.Depends("$PROG_PATH", dep)

linker_arguments = extract_link_args(prebuilt_config)

# remove the main linker script flags '-T linker.cmd'
try:
    ld_index = linker_arguments["link_flags"].index("linker.cmd")
    linker_arguments["link_flags"].pop(ld_index)
    linker_arguments["link_flags"].pop(ld_index - 1)
except:
    pass

# Flags shouldn't be merged automatically as they have precise position in linker cmd
ignore_flags = ("CMakeFiles", "-Wl,--whole-archive", "-Wl,--no-whole-archive", "-Wl,-T")
linker_arguments["link_flags"] = filter_args(
    linker_arguments["link_flags"], ["-"], ignore_flags
)

#
# On this stage project libraries are placed in proper places inside the linker command
#

process_project_lib_deps(
    framework_modules_map,
    linker_arguments["project_libs"],
    preliminary_elf_path,
    offsets_lib,
    linker_arguments["lib_paths"],
)

#
# Here default build flags pulled from the `app` configuration
#

env.Replace(ARFLAGS=["qc"])
env.Append(
    CPPPATH=app_includes["plain_includes"],
    CCFLAGS=[("-isystem", inc) for inc in app_includes.get("sys_includes", [])],
    CPPDEFINES=get_app_defines(app_config),
    LINKFLAGS=linker_arguments["link_flags"],
)

build_flags = get_default_build_flags(
    app_config, get_default_module_config(target_configs)
)
env.Append(**build_flags)


#
# Custom builders required
#

env.Append(
    BUILDERS=dict(
        ElfToBin=Builder(
            action=env.VerboseAction(
                " ".join(
                    [
                        "$OBJCOPY",
                        "--gap-fill",
                        "0xff",
                        "--remove-section=.comment",
                        "--remove-section=COMMON",
                        "--remove-section=.eh_frame",
                        "-O",
                        "binary",
                        "$SOURCES",
                        "$TARGET",
                    ]
                ),
                "Building $TARGET",
            ),
            suffix=".bin",
        ),
        ElfToHex=Builder(
            action=env.VerboseAction(
                " ".join(
                    [
                        "$OBJCOPY",
                        "-O",
                        "ihex",
                        "--remove-section=.comment",
                        "--remove-section=COMMON",
                        "--remove-section=.eh_frame",
                        "$SOURCES",
                        "$TARGET",
                    ]
                ),
                "Building $TARGET",
            ),
            suffix=".hex",
        ),
    )
)

if get_board_architecture(board) == "arm":
    env.Replace(
        SIZEPROGREGEXP=r"^(?:text|_TEXT_SECTION_NAME_2|sw_isr_table|devconfig|rodata|\.ARM.exidx)\s+(\d+).*",
        SIZEDATAREGEXP=r"^(?:datas|bss|noinit|initlevel|_k_mutex_area|_k_stack_area)\s+(\d+).*",
    )

#
# Target: menuconfig
#

env.AddPlatformTarget(
    "menuconfig",
    None,
    [env.VerboseAction(RunMenuconfig, "Running menuconfig...")],
    "Run Menuconfig",
)
