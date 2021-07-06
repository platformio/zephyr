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
import sys

import click

from SCons.Script import ARGUMENTS, COMMAND_LINE_TARGETS

from platformio import fs
from platformio.compat import WINDOWS
from platformio.proc import exec_command
from platformio.util import get_systype

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
}


def get_board_architecture(board_config):
    if board_config.get("build.cpu", "").lower().startswith("cortex"):
        return "arm"
    elif board_config.get("build.march", "") in ("rv64imac", "rv32im", "rv32ima", "rv32imac"):
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

    return False


def get_zephyr_modules():
    west_config = os.path.join(FRAMEWORK_DIR, "west.yml")
    if not os.path.isfile(west_config):
        sys.stderr.write("Error: Couldn't find 'west.yml'\n")
        env.Exit(1)

    with open(west_config) as fp:
        config = list(yaml.load_all(fp, Loader=yaml.FullLoader))[0]
        return [
            m
            for m in config["manifest"]["projects"]
            if not m["path"].startswith(
                ("tools", "modules/bsim_hw_models", "modules/hal")
            )
        ]


def run_cmake():
    print("Reading CMake configuration...")

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
    ]

    menuconfig_file = os.path.join(PROJECT_DIR, "zephyr", "menuconfig.conf")
    if os.path.isfile(menuconfig_file):
        print("Adding -DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)
        cmake_cmd.append("-DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)

    if board.get("build.zephyr.cmake_extra_args", ""):
        cmake_cmd.extend(
            click.parser.split_arg_string(board.get("build.zephyr.cmake_extra_args"))
        )

    zephyr_modules = []
    for m in get_zephyr_modules():
        module_name = "framework-zephyr-" + m["name"].replace("_", "-")
        try:
            module_path = platform.get_package_dir(module_name)
        except KeyError:
            print("Warning! Missing Zephyr module " + module_name)
            continue
        zephyr_modules.append(module_path)

    platform_name = env.subst("$PIOPLATFORM")
    if platform_name in PLATFORMS_WITH_EXTERNAL_HAL:
        zephyr_modules.extend(
            [
                platform.get_package_dir("framework-zephyr-hal-" + m)
                for m in PLATFORMS_WITH_EXTERNAL_HAL[platform_name]
            ]
        )

    if get_board_architecture(board) == "arm":
        zephyr_modules.append(platform.get_package_dir("framework-zephyr-cmsis"))

    if zephyr_modules:
        zephyr_modules = [
            _fix_package_path(m) if "@" in os.path.basename(m) else m
            for m in zephyr_modules
        ]
        cmake_cmd.extend(["-D", "ZEPHYR_MODULES=" + ";".join(zephyr_modules)])

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


def get_cmake_code_model():
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
        run_cmake()

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
        if board.get("build.march", "") == "rv32imac":
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
            os.path.dirname(module_path), module_name.replace("@", "-"),
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


def get_linkerscript_final_cmd(app_config, base_ld_script):
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
        "-E",
        "$SOURCE",
        "-P",
        "-o",
        "$TARGET",
    ]

    includes = extract_includes_from_compile_group(app_config["compileGroups"][0])
    cmd.extend(['-I"%s"' % inc for inc in includes["plain_includes"]])

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "linker_pass_final.cmd"),
        base_ld_script,
        env.VerboseAction(" ".join(cmd), "Generating final linker script $TARGET"),
    )


def find_base_ldscript(app_config):
    includes = extract_includes_from_compile_group(app_config["compileGroups"][0])
    # A temporary solution since there is no easy way to find linker script
    for inc in includes["plain_includes"]:
        for f in os.listdir(inc):
            if f == "linker.ld" and os.path.isfile(os.path.join(inc, f)):
                return os.path.join(inc, f)

    sys.stderr.write("Error: Couldn't find a base linker script!\n")
    env.Exit(1)


def get_linkerscript_cmd(app_config, base_ld_script):
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
        "-E",
        "$SOURCE",
        "-P",
        "-o",
        "$TARGET",
    ]

    includes = extract_includes_from_compile_group(app_config["compileGroups"][0])
    cmd.extend(['-I"%s"' % inc for inc in includes["plain_includes"]])

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "linker.cmd"),
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
    includes = extract_includes_from_compile_group(
        app_config["compileGroups"][0], FRAMEWORK_DIR)
    # Add path with pregenerated headers
    includes["plain_includes"].append(
        os.path.join(BUILD_DIR, "zephyr", "include", "generated")
    )

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


def get_app_flags(app_config, default_env):
    app_env = prepare_build_envs(app_config, default_env)[0]
    return {
        "ASFLAGS": app_env.get("ASFLAGS", []),
        "CFLAGS": app_env.get("CFLAGS", []),
        "CCFLAGS": app_env.get("CCFLAGS", []),
        "CXXFLAGS": app_env.get("CXXFLAGS", []),
    }


def extract_link_args(target_config):
    link_args = {"LINKFLAGS": [], "LIBS": [], "LIBPATH": [], "__LIB_DEPS": []}

    for f in target_config.get("link", {}).get("commandFragments", []):
        fragment = f.get("fragment", "").strip()
        fragment_role = f.get("role", "").strip()
        if not fragment or not fragment_role:
            continue
        args = click.parser.split_arg_string(fragment)
        if fragment_role == "flags":
            link_args["LINKFLAGS"].extend(args)
        elif fragment_role == "libraries":
            if fragment.startswith("-l"):
                link_args["LIBS"].extend(args)
            elif fragment.startswith("-L"):
                lib_path = fragment.replace("-L", "").strip()
                if lib_path not in link_args["LIBPATH"]:
                    link_args["LIBPATH"].append(lib_path.replace('"', ""))
            elif fragment.startswith("-") and not fragment.startswith("-l"):
                # CMake mistakenly marks LINKFLAGS as libraries
                link_args["LINKFLAGS"].extend(args)
            elif os.path.isfile(fragment) and os.path.isabs(fragment):
                # In case of precompiled archives from framework package
                lib_path = os.path.dirname(fragment)
                if lib_path not in link_args["LIBPATH"]:
                    link_args["LIBPATH"].append(os.path.dirname(fragment))
                link_args["LIBS"].extend(
                    [os.path.basename(l) for l in args if l.endswith(".a")]
                )
            elif fragment.endswith(".a"):
                link_args["__LIB_DEPS"].extend(
                    [os.path.basename(l) for l in args if l.endswith(".a")]
                )
            else:
                link_args["LINKFLAGS"].extend(args)

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


def generate_isr_table_file_cmd(preliminary_elf, board_config):
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

    config_file = os.path.join(BUILD_DIR, "zephyr", ".config")

    if os.path.isfile(config_file):
        with open(config_file) as fp:
            data = fp.read()
        if "CONFIG_GEN_ISR_TABLES=y" in data:
            cmd.append("--sw-isr-table")
        if "CONFIG_GEN_IRQ_VECTOR_TABLE=y" in data:
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
    autoconf = os.path.join(BUILD_DIR, "zephyr", "include", "generated", "autoconf.h")
    if not os.path.isfile(autoconf):
        print("Warning! Cannot find autoconf file. Project settings won't be processed")
        return result
    with open(autoconf, "r") as fp:
        for line in fp.readlines():
            line = line.strip()
            if line.startswith("#define"):
                config = line.split(" ", 2)
                assert len(config) != 2, config
                result[config[1]] = config[2]
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

#
# Current build script limitations
#

env.EnsurePythonVersion(3, 4)

if " " in FRAMEWORK_DIR:
    sys.stderr.write("Error: Detected a whitespace character in framework path\n")
    env.Exit(1)

#
# Initial targets loading
#

codemodel = get_cmake_code_model()
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
base_ld_script = find_base_ldscript(app_config)
final_ld_script = get_linkerscript_final_cmd(app_config, base_ld_script)
preliminary_ld_script = get_linkerscript_cmd(app_config, base_ld_script)

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
    if target_config["type"] not in (
        "STATIC_LIBRARY",
        "OBJECT_LIBRARY",
    ) or lib_name in ("app", "offsets"):
        continue

    lib = build_library(env, target_config, PROJECT_SRC_DIR)
    framework_modules_map[target_config["id"]] = lib

    if any(
        d.get("id", "").startswith(("zephyr_generated_headers"))
        for d in target_config.get("dependencies", [])
    ):
        env.Depends(lib[0].sources, offset_header_file)

# Offsets library compiled separately as it used later for custom dependencies
offsets_lib = build_library(env, target_configs["offsets"], PROJECT_SRC_DIR)

#
# Preliminary elf and subsequent targets
#

preliminary_elf_path = os.path.join("$BUILD_DIR", "firmware-pre.elf")

for dep in (offsets_lib, preliminary_ld_script):
    env.Depends(preliminary_elf_path, dep)

isr_table_file = generate_isr_table_file_cmd(preliminary_elf_path, board)

#
# Final firmware targets
#

env.Append(
    PIOBUILDFILES=compile_source_files(prebuilt_config, env, PROJECT_SRC_DIR),
    _EXTRA_ZEPHYR_PIOBUILDFILES=compile_source_files(
        target_configs["zephyr_final"], env, PROJECT_SRC_DIR
    ),
    __ZEPHYR_OFFSET_HEADER_CMD=offset_header_file
)

for dep in (isr_table_file, final_ld_script):
    env.Depends("$PROG_PATH", dep)

# Note: `app` is replaced by files from the `src` folder
# `kernel`, `libopenthread` and `isr_tables` libs should be placed outside the
# `whole-archive` flags
ignore_libs = ("kernel", "isr_tables", "app", "openthread")

whole_libs = []
weak_libs = []
for d in prebuilt_config.get("dependencies", []):
    dependency_id = d["id"]
    if not framework_modules_map.get(dependency_id, {}):
        continue

    fmodule = framework_modules_map[dependency_id]
    if dependency_id.startswith(ignore_libs):
        weak_libs.append(fmodule)
    else:
        whole_libs.append(fmodule)

env.Replace(ARFLAGS=["qc"])
env.Prepend(_LIBFLAGS="-Wl,--whole-archive ")

project_config = app_config
project_defines = get_app_defines(project_config)
project_flags = get_app_flags(project_config, env)
link_args = extract_link_args(prebuilt_config)

# remove the main linker script flags '-T linker.cmd'
try:
    ld_index = link_args["LINKFLAGS"].index("linker.cmd")
    link_args["LINKFLAGS"].pop(ld_index)
    link_args["LINKFLAGS"].pop(ld_index - 1)
except:
    print("Warning! Couldn't find the main linker script in the CMake code model.")

# Flags shouldn't be merged automatically as they have precise position in linker cmd
ignore_flags = ("CMakeFiles", "-Wl,--whole-archive", "-Wl,--no-whole-archive")

link_args["LINKFLAGS"] = sorted(
    filter_args(link_args["LINKFLAGS"], ["-"], ignore_flags)
)

# Note: isr_tables, kernel, openthread and standard libraries must be placed explicitly
# after zephyr libraries outside of whole-archive flag and specified as explicit
# dependencies

env.Depends(
    preliminary_elf_path, [env.subst(library)[0] for library in weak_libs]
)

env.Append(
    CPPPATH=app_includes["plain_includes"],
    CCFLAGS=[("-isystem", inc) for inc in app_includes.get("sys_includes", [])],
    CPPDEFINES=project_defines,
    LIBS=sorted(whole_libs) + [offsets_lib],
    _LIBFLAGS=" -Wl,--no-whole-archive "
    + " ".join([env.subst(library)[0] for library in weak_libs] + link_args["LIBS"]),
)

# Standard libraries in LIBS are already added to the LINKCOMMAND in _LIBFLAGS
link_args["LIBS"] = []
project_flags.update(link_args)
env.MergeFlags(project_flags)

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
